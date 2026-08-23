"""全语料 BM25 召回与 RRF 双路排名融合。"""

# Counter 保存每个知识切片的词频，BM25 查询时无需重复扫描并统计正文。
from collections import Counter

# dataclass 用一个内部小对象汇总同一切片在两条召回榜中的信息。
from dataclasses import dataclass

# log 用于计算 BM25 的逆文档频率，使少见业务词比常见字更有区分度。
from math import log

# Literal 让本地通道列表与 RetrievalHit 的有限字符串类型完全一致。
from typing import Literal

# KnowledgeChunk 是建索引时的完整语料；RetrievalHit 是交给 LangGraph 的统一结果。
from serviceops_agent.domain.knowledge import KnowledgeChunk, RetrievalHit

# 中文词面切分器与旧 BM25 重排共用，保证对照实验只改变召回范围而不改变分词规则。
from serviceops_agent.rag.reranking import ChineseLexicalTokenizer

# KnowledgeRetriever 让向量路可以是本地 Qdrant、独立服务或测试替身。
from serviceops_agent.rag.retriever import KnowledgeRetriever


class BM25CorpusRetriever:
    """独立扫描整份知识切片语料，而不是只重排向量候选。"""

    def __init__(
        self,
        *,
        chunks: list[KnowledgeChunk],
        k1: float = 1.5,
        b: float = 0.75,
        tokenizer: ChineseLexicalTokenizer | None = None,
    ) -> None:
        """在应用启动时预计算全语料词频、文档频率和平均长度。"""

        # 空语料无法形成 IDF 和平均长度，说明知识治理或切片阶段配置有误。
        if not chunks:
            raise ValueError("BM25 全语料索引不能使用空知识切片")
        # k1 控制重复词贡献的饱和速度，必须为正数。
        if k1 <= 0.0:
            raise ValueError("BM25 k1 必须大于 0")
        # b 控制长短文档归一化，只接受标准的零到一范围。
        if not 0.0 <= b <= 1.0:
            raise ValueError("BM25 b 必须在 0 到 1 之间")

        # 使用列表副本冻结当前索引语料顺序，避免调用方后续修改原列表。
        self._chunks = list(chunks)
        # 保存标准 BM25 的词频饱和参数。
        self._k1 = k1
        # 保存标准 BM25 的文档长度归一参数。
        self._b = b
        # 默认采用对中英文混合客服问题友好的单字、双字和英文词切分。
        self._tokenizer = tokenizer or ChineseLexicalTokenizer()
        # 标题与正文共同建立词面索引，政策名和正文细节都能被精确召回。
        tokenized_documents = [
            self._tokenizer.tokenize(chunk.embedding_text()) for chunk in self._chunks
        ]
        # 每份切片的 Counter 在查询期直接复用，避免每个请求重新统计词频。
        self._term_frequencies = [Counter(tokens) for tokens in tokenized_documents]
        # 保存每份切片的词元数，用于抑制长文档因词多而虚高的匹配分数。
        self._document_lengths = [len(tokens) for tokens in tokenized_documents]
        # 语料非空，因此平均长度始终有定义；至少按 1 处理极端空分词文本。
        self._average_document_length = max(
            1.0,
            sum(self._document_lengths) / len(self._document_lengths),
        )
        # document_frequency 统计一个词出现在多少份切片，而不是总共出现多少次。
        self._document_frequency: Counter[str] = Counter()
        # 逐份文档使用 set 去重，同一个词在一份文档重复多次只增加一次文档频率。
        for tokens in tokenized_documents:
            self._document_frequency.update(set(tokens))

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """从全语料独立返回 BM25 排名，不依赖 Qdrant 候选集合。"""

        # 非法 Top-K 属于程序配置错误，应尽早暴露而不是静默返回空结果。
        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        # 查询词只需计算一次贡献；重复输入不会被人为重复加权。
        query_terms = set(self._tokenizer.tokenize(query))
        # 空白或无法分词的输入没有词面证据，不应该制造候选。
        if not query_terms:
            return []

        # scored_chunks 保存 BM25 原始分数和切片，稍后按分数稳定排序。
        scored_chunks: list[tuple[float, KnowledgeChunk]] = []
        # 同时遍历切片、词频和长度，三者在构造阶段保持相同顺序。
        for chunk, frequencies, document_length in zip(
            self._chunks,
            self._term_frequencies,
            self._document_lengths,
            strict=True,
        ):
            # 当前切片从零开始累计所有查询词的 BM25 贡献。
            score = 0.0
            # 查询集合中的每个词最多计算一次。
            for term in query_terms:
                # 当前文档不包含该词时没有贡献。
                frequency = frequencies.get(term, 0)
                if frequency == 0:
                    continue
                # 全语料 IDF 让“运费险”等少见术语比“可以”等常见词更重要。
                inverse_document_frequency = log(
                    1.0
                    + (len(self._chunks) - self._document_frequency[term] + 0.5)
                    / (self._document_frequency[term] + 0.5)
                )
                # 长度归一项避免较长切片单纯因为包含更多字而占优势。
                length_normalization = self._k1 * (
                    1.0
                    - self._b
                    + self._b * document_length / self._average_document_length
                )
                # 标准 BM25 词频项会随着重复次数增加逐渐饱和。
                score += inverse_document_frequency * (
                    frequency * (self._k1 + 1.0)
                    / (frequency + length_normalization)
                )
            # 零分表示没有任何可解释的词面重合，不进入关键词召回榜。
            if score > 0.0:
                scored_chunks.append((score, chunk))

        # 先按分数降序，再以稳定 chunk_id 打破同分，保证测试和线上结果可复现。
        ranked_chunks = sorted(
            scored_chunks,
            key=lambda item: (-item[0], item[1].chunk_id),
        )
        # 没有任何词面命中时直接返回空列表。
        if not ranked_chunks:
            return []
        # 用本次最高 BM25 分数映射到零到一，只为兼容统一 RetrievalHit.score。
        maximum_score = ranked_chunks[0][0]
        # 返回调用方要求的关键词候选，同时保留未经缩放的原始 BM25 分数。
        return [
            RetrievalHit(
                chunk=chunk,
                score=raw_score / maximum_score,
                lexical_score=raw_score,
                lexical_rank=rank,
                retrieval_channels=["lexical"],
            )
            for rank, (raw_score, chunk) in enumerate(ranked_chunks[:top_k], start=1)
        ]


@dataclass(slots=True)
class _FusionCandidate:
    """RRF 合并同一切片两路信息时使用的内部临时记录。"""

    # chunk 保存最终返回的原始受治理证据。
    chunk: KnowledgeChunk
    # dense_score 是向量通道原始余弦分数。
    dense_score: float | None = None
    # lexical_score 是关键词通道原始 BM25 分数。
    lexical_score: float | None = None
    # dense_rank 是向量榜一基排名。
    dense_rank: int | None = None
    # lexical_rank 是关键词榜一基排名。
    lexical_rank: int | None = None


class ReciprocalRankFusionRetriever:
    """分别执行全库向量召回和全库 BM25 召回，再用 RRF 合并名次。"""

    def __init__(
        self,
        *,
        dense_retriever: KnowledgeRetriever,
        lexical_retriever: BM25CorpusRetriever,
        dense_k: int,
        lexical_k: int,
        rrf_k: int,
        dense_weight: float,
        lexical_weight: float,
    ) -> None:
        """保存两条独立召回通道与冻结后的融合参数。"""

        # 两条通道都必须真实取回至少一条候选。
        if dense_k < 1 or lexical_k < 1:
            raise ValueError("混合召回的两路候选数都必须大于等于 1")
        # RRF 常数必须为正，避免分母为零并抑制榜首过度放大。
        if rrf_k < 1:
            raise ValueError("RRF k 必须大于等于 1")
        # 两路权重都必须为正，否则配置名为混合召回却实际关闭了某一路。
        if dense_weight <= 0.0 or lexical_weight <= 0.0:
            raise ValueError("RRF 两路权重都必须大于 0")
        # Qdrant 检索器负责语义相似问题的独立全库召回。
        self._dense_retriever = dense_retriever
        # BM25 检索器负责精确业务词的独立全库召回。
        self._lexical_retriever = lexical_retriever
        # 保存向量通道候选池大小。
        self._dense_k = dense_k
        # 保存关键词通道候选池大小。
        self._lexical_k = lexical_k
        # 保存 RRF 排名平滑常数。
        self._rrf_k = rrf_k
        # 保存向量榜贡献权重。
        self._dense_weight = dense_weight
        # 保存关键词榜贡献权重。
        self._lexical_weight = lexical_weight

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """独立取回两份榜单，以 chunk_id 去重并返回融合后的 Top-K。"""

        # 最终返回数量必须为正数。
        if top_k < 1:
            raise ValueError("top_k 必须大于等于 1")
        # 向量路从 Qdrant 的整座 Collection 中独立寻找候选。
        dense_hits = self._dense_retriever.search(
            query,
            top_k=max(top_k, self._dense_k),
        )
        # 关键词路从完整 BM25 语料中独立寻找候选，不读取 dense_hits。
        lexical_hits = self._lexical_retriever.search(
            query,
            top_k=max(top_k, self._lexical_k),
        )
        # candidates 使用稳定 chunk_id 合并同一份证据，避免两路共同命中时重复返回。
        candidates: dict[str, _FusionCandidate] = {}
        # 按真实返回顺序重新编号，防止测试替身未填 dense_rank。
        for rank, hit in enumerate(dense_hits, start=1):
            candidates[hit.chunk.chunk_id] = _FusionCandidate(
                chunk=hit.chunk,
                dense_score=hit.dense_score if hit.dense_score is not None else hit.score,
                dense_rank=rank,
            )
        # 把 BM25 榜加入同一个候选字典；已存在时只补词面字段。
        for rank, hit in enumerate(lexical_hits, start=1):
            candidate = candidates.setdefault(
                hit.chunk.chunk_id,
                _FusionCandidate(chunk=hit.chunk),
            )
            candidate.lexical_score = (
                hit.lexical_score if hit.lexical_score is not None else hit.score
            )
            candidate.lexical_rank = rank

        # 理论最高分是同一切片同时位于两张榜的第一名，用它把 RRF 映射到零到一。
        maximum_rrf_score = (
            self._dense_weight + self._lexical_weight
        ) / (self._rrf_k + 1)
        # fused_hits 保存可解释领域结果，排序时不再依赖原字典插入顺序。
        fused_hits: list[RetrievalHit] = []
        # 逐个候选累计它在两张榜上的倒数名次贡献。
        for candidate in candidates.values():
            # 当前切片从零开始累计 RRF 分数。
            raw_rrf_score = 0.0
            # 进入向量榜时增加向量权重除以平滑后的名次。
            if candidate.dense_rank is not None:
                raw_rrf_score += self._dense_weight / (
                    self._rrf_k + candidate.dense_rank
                )
            # 进入关键词榜时增加关键词权重除以平滑后的名次。
            if candidate.lexical_rank is not None:
                raw_rrf_score += self._lexical_weight / (
                    self._rrf_k + candidate.lexical_rank
                )
            # 按固定顺序记录实际命中的通道，页面无需猜测空字段。
            channels: list[Literal["dense", "lexical"]] = []
            # 向量名次存在时追加受 Literal 约束的 dense 标记。
            if candidate.dense_rank is not None:
                channels.append("dense")
            # 关键词名次存在时追加受 Literal 约束的 lexical 标记。
            if candidate.lexical_rank is not None:
                channels.append("lexical")
            # 组装最终命中，保留每条通道的原始分数、排名和融合方法。
            fused_hits.append(
                RetrievalHit(
                    chunk=candidate.chunk,
                    score=min(1.0, raw_rrf_score / maximum_rrf_score),
                    dense_score=candidate.dense_score,
                    lexical_score=candidate.lexical_score,
                    dense_rank=candidate.dense_rank,
                    lexical_rank=candidate.lexical_rank,
                    retrieval_channels=channels,
                    fusion_method="rrf",
                )
            )

        # 最终按 RRF 分数降序；同分优先双路命中，再比较两路名次和稳定 ID。
        ranked_hits = sorted(
            fused_hits,
            key=lambda hit: (
                -hit.score,
                -len(hit.retrieval_channels),
                hit.dense_rank or 10**9,
                hit.lexical_rank or 10**9,
                hit.chunk.chunk_id,
            ),
        )
        # 只把最终 Top-K 交给证据充分性和回答节点，控制上下文噪声。
        return ranked_hits[:top_k]

    def health_check(self) -> None:
        """由向量通道探测独立 Qdrant；BM25 索引与当前 Python 进程同生共死。"""

        # 默认生产向量检索器实现 health_check；getattr 让错误注入测试仍可明确失败。
        health_check = getattr(self._dense_retriever, "health_check", None)
        # 缺少探测能力说明装配的不是可用于生产就绪检查的检索器。
        if not callable(health_check):
            raise RuntimeError("向量检索器未提供 Qdrant 健康检查")
        # 调用 Qdrant Collection 只读探测，异常交给 /ready 转换为 503。
        health_check()
