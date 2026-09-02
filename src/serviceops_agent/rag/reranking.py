"""候选集合内的中文BM25词面重排与检索器装饰器。"""

# re提取英文数字词元和连续中文片段。
import re

# Counter统计候选切片词频；log计算BM25逆文档频率。
from collections import Counter
from math import isfinite, log
from typing import Protocol

import httpx
from pydantic import BaseModel, Field, TypeAdapter

# RetrievalHit保存重排后的同一证据切片和融合分数。
from serviceops_agent.domain.knowledge import RetrievalHit

# KnowledgeRetriever协议允许包装Qdrant或测试替身。
from serviceops_agent.rag.retriever import KnowledgeRetriever


class CandidateReranker(Protocol):
    """候选闭包内重排器的最小协议。"""

    def rerank(
        self,
        *,
        query: str,
        hits: list[RetrievalHit],
        top_k: int,
    ) -> list[RetrievalHit]:
        """只改变传入候选顺序和排序分数。"""


class CrossEncoderScoringClient(Protocol):
    """把查询与候选文本联合编码并返回原顺序相关性分数。"""

    def score(self, *, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长、位于 0 到 1 的分数。"""


class CrossEncoderServiceError(RuntimeError):
    """外部重排服务不可用或响应不满足闭包契约。"""


class _TEIRank(BaseModel):
    """Hugging Face TEI /rerank 的单条有限响应。"""

    index: int = Field(ge=0)
    score: float = Field(ge=0.0, le=1.0)


class TEICrossEncoderScoringClient:
    """调用独立 Hugging Face Text Embeddings Inference Cross-Encoder。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("TEI base_url 必须使用 http:// 或 https://")
        if timeout_seconds <= 0.0:
            raise ValueError("TEI timeout_seconds 必须大于 0")
        normalized = base_url.rstrip("/")
        self._url = normalized if normalized.endswith("/rerank") else f"{normalized}/rerank"
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def score(self, *, query: str, documents: list[str]) -> list[float]:
        """请求归一化分数，并按输入索引恢复原候选顺序。"""

        if not documents:
            return []
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(
                    self._url,
                    headers=headers,
                    json={
                        "query": query,
                        "texts": documents,
                        "raw_scores": False,
                        "return_text": False,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            raw_ranks = payload.get("ranks") if isinstance(payload, dict) else payload
            ranks = TypeAdapter(list[_TEIRank]).validate_python(raw_ranks)
            scores: list[float | None] = [None] * len(documents)
            for rank in ranks:
                if rank.index >= len(documents) or scores[rank.index] is not None:
                    raise ValueError("TEI 返回重复或越界候选索引")
                scores[rank.index] = rank.score
            if any(score is None for score in scores):
                raise ValueError("TEI 返回候选数量不完整")
            return [float(score) for score in scores if score is not None]
        except Exception as error:
            # 不传播第三方响应正文、URL 查询参数或鉴权信息。
            raise CrossEncoderServiceError("Cross-Encoder 重排服务暂不可用") from error


class CrossEncoderCandidateReranker:
    """用联合编码相关性重排第一阶段候选，但不创建或补召回证据。"""

    def __init__(self, *, scoring_client: CrossEncoderScoringClient) -> None:
        self._scoring_client = scoring_client

    def rerank(
        self,
        *,
        query: str,
        hits: list[RetrievalHit],
        top_k: int,
    ) -> list[RetrievalHit]:
        """批量评分全部候选，稳定按相关性降序返回。"""

        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        if not hits:
            return []
        scores = self._scoring_client.score(
            query=query,
            documents=[f"{hit.chunk.title}\n{hit.chunk.content}" for hit in hits],
        )
        if len(scores) != len(hits) or any(
            not isfinite(score) or not 0.0 <= score <= 1.0 for score in scores
        ):
            raise CrossEncoderServiceError("Cross-Encoder 返回了非法相关性分数")
        ranked = sorted(
            enumerate(zip(hits, scores, strict=True)),
            key=lambda item: (-item[1][1], item[0]),
        )
        return [
            RetrievalHit(
                chunk=hit.chunk,
                score=score,
                dense_score=hit.dense_score,
                lexical_score=hit.lexical_score,
                dense_rank=hit.dense_rank,
                lexical_rank=hit.lexical_rank,
                retrieval_channels=hit.retrieval_channels,
                fusion_method="cross_encoder",
            )
            for _, (hit, score) in ranked[:top_k]
        ]


class ChineseLexicalTokenizer:
    """为短中文查询生成英文词、中文单字和相邻双字词面特征。"""

    def tokenize(self, text: str) -> list[str]:
        """返回允许重复的词元列表，让词频参与BM25。"""

        # lower统一英文大小写，不改变中文。
        normalized = text.lower()
        # 连续英文和数字保留完整形式，例如VIP、Top5或订单号。
        tokens = re.findall(r"[a-z0-9]+", normalized)
        # 每个连续中文片段分别生成局部特征。
        for block in re.findall(r"[\u4e00-\u9fff]+", normalized):
            # 单字提高口语短查询的基本重合机会。
            tokens.extend(block)
            # 双字保留“发票”“签收”“红冲”等更有区分度的短语。
            tokens.extend(block[index : index + 2] for index in range(len(block) - 1))
        # 返回原始重复词元，不提前去重。
        return tokens


class BM25CandidateReranker:
    """在原始向量候选内融合BM25词面分数，不新增任何证据。"""

    def __init__(
        self,
        *,
        lexical_weight: float,
        k1: float = 1.5,
        b: float = 0.75,
        tokenizer: ChineseLexicalTokenizer | None = None,
    ) -> None:
        """保存融合权重和标准BM25长度归一参数。"""

        # lexical_weight必须在0到1之间，否则两路融合失去清晰解释。
        if not 0.0 <= lexical_weight <= 1.0:
            # 启动或实验构建阶段立即暴露非法权重。
            raise ValueError("lexical_weight 必须在 0 到 1 之间")
        # k1必须为正数，控制词频饱和速度。
        if k1 <= 0.0:
            # 非法值会使BM25分母失去定义。
            raise ValueError("BM25 k1 必须大于 0")
        # b限制在0到1之间，控制文档长度归一强度。
        if not 0.0 <= b <= 1.0:
            # 超出范围不再是常规BM25长度校正。
            raise ValueError("BM25 b 必须在 0 到 1 之间")
        # _lexical_weight表示最终分数中BM25归一分数的占比。
        self._lexical_weight = lexical_weight
        # _dense_weight是原Qdrant余弦分数占比，两者之和固定为1。
        self._dense_weight = 1.0 - lexical_weight
        # _k1保存词频饱和参数。
        self._k1 = k1
        # _b保存长度归一参数。
        self._b = b
        # 未注入替身时使用项目统一中文词面切分器。
        self._tokenizer = tokenizer or ChineseLexicalTokenizer()

    def rerank(
        self,
        *,
        query: str,
        hits: list[RetrievalHit],
        top_k: int,
    ) -> list[RetrievalHit]:
        """只重排输入hits，并返回最多top_k条融合结果。"""

        # top_k小于1属于调用方配置错误，不能静默返回空列表。
        if top_k < 1:
            # 与KnowledgeRetriever协议的常规约束保持一致。
            raise ValueError("top_k 必须大于等于 1")
        # 空候选无需计算词频和IDF。
        if not hits:
            # 返回新空列表表达没有证据。
            return []
        # 单候选不需要排序，但仍限制返回数量并复制列表。
        if len(hits) == 1:
            # 保留原证据和分数。
            return list(hits[:top_k])

        # query_tokens允许重复；BM25查询项只需按集合遍历一次。
        query_tokens = self._tokenizer.tokenize(query)
        # document_tokens把标题和切片正文共同视为候选文本。
        document_tokens = [
            # 标题帮助区分相邻政策主题，换行保持边界可读。
            self._tokenizer.tokenize(f"{hit.chunk.title}\n{hit.chunk.content}")
            # 遍历原始向量顺序。
            for hit in hits
        ]
        # term_frequencies为每个候选保存重复词元计数。
        term_frequencies = [Counter(tokens) for tokens in document_tokens]
        # document_lengths记录BM25长度归一需要的词元数。
        document_lengths = [len(tokens) for tokens in document_tokens]
        # average_document_length是当前候选集合的平均长度。
        average_document_length = sum(document_lengths) / len(document_lengths)
        # query_term_set去重查询项，避免查询重复词被重复累计两次。
        query_term_set = set(query_tokens)
        # document_frequencies记录每个查询词出现在多少个候选中。
        document_frequencies = {
            # 当前词的文档频率通过候选词频字典成员判断。
            token: sum(1 for frequencies in term_frequencies if token in frequencies)
            # 只计算本次查询真正使用的词。
            for token in query_term_set
        }
        # lexical_scores按原候选顺序保存BM25原始分数。
        lexical_scores: list[float] = []
        # 逐候选计算词面相关性。
        for frequencies, document_length in zip(
            term_frequencies,
            document_lengths,
            strict=True,
        ):
            # score从零开始累计每个查询词贡献。
            score = 0.0
            # 遍历查询去重词元。
            for token in query_term_set:
                # frequency是当前候选中的词频，不存在时为零。
                frequency = frequencies.get(token, 0)
                # 不存在的词对当前候选没有贡献。
                if frequency == 0:
                    # 继续计算下一个查询词。
                    continue
                # document_frequency至少为1，因为当前候选包含该词。
                document_frequency = document_frequencies[token]
                # idf使用始终为正的BM25常见平滑形式。
                inverse_document_frequency = log(
                    1.0
                    + (len(hits) - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                # length_normalization惩罚只因文本较长而匹配更多常用词的候选。
                length_normalization = self._k1 * (
                    1.0
                    - self._b
                    + self._b * document_length / average_document_length
                )
                # 标准BM25词频项在高重复次数时逐渐饱和。
                term_score = (
                    frequency * (self._k1 + 1.0)
                    / (frequency + length_normalization)
                )
                # 当前查询词贡献等于IDF乘饱和词频分数。
                score += inverse_document_frequency * term_score
            # 保存当前候选原始BM25分数。
            lexical_scores.append(score)

        # max_lexical_score用于把BM25分数缩放到0到1。
        max_lexical_score = max(lexical_scores)
        # combined_hits保存原始位置、融合分数和原命中。
        combined_hits: list[tuple[int, float, RetrievalHit]] = []
        # 同时遍历原始命中和对应词面分数。
        for original_index, (hit, lexical_score) in enumerate(
            zip(hits, lexical_scores, strict=True)
        ):
            # Qdrant余弦范围约为-1到1，线性映射到0到1。
            normalized_dense_score = max(0.0, min(1.0, (hit.score + 1.0) / 2.0))
            # 全部BM25为零时保持零，避免除零。
            normalized_lexical_score = (
                # 当前词面分数除以候选最大值。
                lexical_score / max_lexical_score
                # 只有最大值为正时执行缩放。
                if max_lexical_score > 0.0
                # 查询与候选完全无词面重合时BM25贡献为零。
                else 0.0
            )
            # 融合分数是两路归一分数的可解释加权和。
            combined_score = (
                self._dense_weight * normalized_dense_score
                + self._lexical_weight * normalized_lexical_score
            )
            # 保留原始位置作为完全同分时的稳定次级顺序。
            combined_hits.append((original_index, combined_score, hit))
        # 按融合分数降序；同分时保持更早的原向量排名。
        ranked_hits = sorted(
            combined_hits,
            key=lambda item: (-item[1], item[0]),
        )
        # 重新构造RetrievalHit，使下游看到融合分数和完全相同的证据切片。
        return [
            RetrievalHit(
                # chunk对象原样复用，重排绝不创造新证据。
                chunk=hit.chunk,
                # 融合分数已经限制在0到1，满足领域Schema。
                score=combined_score,
                # 保留原始向量分数，避免旧重排模式丢失第一阶段依据。
                dense_score=hit.dense_score if hit.dense_score is not None else hit.score,
                # 当前 BM25 只在向量候选内部计算，因此不声称拥有独立 lexical_rank。
                retrieval_channels=["dense", "lexical"],
                # 明确这是旧候选内 BM25，而不是完整双路 RRF。
                fusion_method="candidate_bm25",
            )
            # 只返回调用方需要的前top_k条。
            for _, combined_score, hit in ranked_hits[:top_k]
        ]


class RerankingKnowledgeRetriever:
    """先使用原检索器召回固定候选，再执行候选内BM25重排。"""

    def __init__(
        self,
        *,
        retriever: KnowledgeRetriever,
        reranker: CandidateReranker,
        candidate_k: int,
    ) -> None:
        """保存底层检索器、重排器和固定候选池大小。"""

        # candidate_k至少为1，否则没有可重排候选。
        if candidate_k < 1:
            # 构建阶段立即拒绝非法候选池。
            raise ValueError("candidate_k 必须大于等于 1")
        # _retriever负责原始Hash/Qdrant召回。
        self._retriever = retriever
        # _reranker只改变已有候选顺序。
        self._reranker = reranker
        # _candidate_k在实验中固定为5，避免扩大召回混入新变量。
        self._candidate_k = candidate_k

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """召回固定候选池并返回重排后的Top-K。"""

        # 请求底层返回固定候选数；top_k更大时至少满足调用方数量。
        candidate_limit = max(self._candidate_k, top_k)
        # raw_hits保留原始向量排序和切片集合。
        raw_hits = self._retriever.search(query, top_k=candidate_limit)
        # rerank只接收raw_hits，不可能引入集合外文档。
        return self._reranker.rerank(query=query, hits=raw_hits, top_k=top_k)

    def health_check(self) -> None:
        """把生产就绪探测委托给底层 Qdrant 检索器。"""

        # 默认底层实现 health_check；测试替身缺失时给出明确装配错误。
        health_check = getattr(self._retriever, "health_check", None)
        # 不把没有健康探测的自定义对象误判为可用于生产。
        if not callable(health_check):
            raise RuntimeError("底层向量检索器未提供 Qdrant 健康检查")
        # 原样传播 Qdrant 连接或 Collection 异常，由 readiness 统一脱敏处理。
        health_check()
