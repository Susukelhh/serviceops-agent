"""第34步：用事实级金标计算单一“端到端有据回答成功率”。

这一步不再用更多平均指标掩盖最终答案风险。每道题只记零或一：可回答题必须把全部
关键事实答对、引用真正支持这些事实且不出现禁止结论；知识缺口题必须安全拒答。
"""

# json负责读取版本化配置与私有盲测文件；re/unicodedata用于稳定文本归一化。
import json
import re
import unicodedata

# Sequence允许评测器接收列表或其他只读样本集合。
from collections.abc import Sequence

# sha256同时冻结语料、盲测文件与候选装配，避免同一路径静默换内容。
from hashlib import sha256

# ceil根据服务商批次上限估算真实Embedding请求次数。
from math import ceil

# Path为配置、语料和私有盲测文件提供跨工作目录定位。
from pathlib import Path

# Literal把逐题结束位置限制为少量可审计值。
from typing import Literal

# Pydantic校验配置、事实规则、逐题结果与最终报告。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# 项目路径解析不依赖PyCharm当前Working directory。
from serviceops_agent.config.paths import resolve_project_path

# Settings提供真实千问密钥、地址、模型、超时与重试参数。
from serviceops_agent.config.settings import Settings

# GroundedAnswerDraft和RetrievalHit复用线上RAG回答与证据边界。
from serviceops_agent.domain.knowledge import GroundedAnswerDraft, RetrievalHit

# 知识仓库在向量化前过滤内部草稿与退役文档。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# 切片器沿用项目冻结的字符窗口与重叠参数。
from serviceops_agent.rag.chunking import KnowledgeChunker

# Hash用于零费用基线；OpenAI兼容客户端用于显式确认后的千问语义向量。
from serviceops_agent.rag.embeddings import (
    EmbeddingClient,
    HashEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)

# Grounded回答器保持和线上相同的结构化输出与证据约束。
from serviceops_agent.rag.generation import (
    GROUNDED_ANSWER_SYSTEM_PROMPT,
    ExtractiveGroundedAnswerClient,
    GroundedAnswerClient,
    create_grounded_answer_client,
)

# BM25和RRF分别完成全库词面召回与双路名次融合。
from serviceops_agent.rag.hybrid import (
    BM25CorpusRetriever,
    ReciprocalRankFusionRetriever,
)

# 确定性范围门在Embedding前拦截明确域外和敏感问题。
from serviceops_agent.rag.query_policy import (
    DeterministicFAQScopePolicy,
    KnowledgeQueryPolicy,
)

# Qdrant检索器、统一协议与内存客户端构成隔离实验索引。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    QdrantKnowledgeRetriever,
    create_qdrant_client,
)


class RequiredFactRule(BaseModel):
    """一个必须同时出现在答案中、且能被实际引用证据支持的原子事实。"""

    # fact_id是失败报告中的稳定标识，不把事实金标正文写入公开报告。
    fact_id: str = Field(min_length=1, max_length=100)
    # answer_all_of中的每一组都必须至少命中一个可接受表述。
    answer_all_of: list[list[str]] = Field(min_length=1, max_length=10)
    # evidence_all_of使用相同“组间AND、组内OR”逻辑验证引用切片确实含有依据。
    evidence_all_of: list[list[str]] = Field(min_length=1, max_length=10)
    # supporting_document_ids限定哪些人工标注文档可以支撑当前事实。
    supporting_document_ids: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_fact_rule(self) -> "RequiredFactRule":
        """拒绝空表述组、重复来源和没有可匹配内容的事实标签。"""

        # 两类匹配规则都不允许出现空组或空白候选词。
        for groups in (self.answer_all_of, self.evidence_all_of):
            # 逐组校验，保证每个AND条件至少有一个可接受表述。
            for group in groups:
                # 空组或全空白组永远无法命中，应在运行前暴露。
                if not group or any(not term.strip() for term in group):
                    # 错误只指出标签结构，不打印私有题目正文。
                    raise ValueError("事实规则不能包含空表述组或空白表述")
        # 来源ID必须去重并按升序保存，使哈希与报告在不同机器上稳定。
        if self.supporting_document_ids != sorted(set(self.supporting_document_ids)):
            # 要求数据作者显式修复标注顺序。
            raise ValueError("supporting_document_ids必须去重并按升序排列")
        # 返回完成组合校验的事实规则。
        return self


class ForbiddenClaimRule(BaseModel):
    """答案中任一命中就形成红线失败的明确错误结论。"""

    # claim_id进入脱敏失败报告，便于回归但不泄漏整条金标。
    claim_id: str = Field(min_length=1, max_length=100)
    # answer_any_of保存几种完整错误表述，避免只匹配单个词造成误伤。
    answer_any_of: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_forbidden_claim(self) -> "ForbiddenClaimRule":
        """禁止空白错误表述和归一化后完全重复的候选。"""

        # 空白字符串会匹配所有答案，因此必须拒绝。
        if any(not term.strip() for term in self.answer_any_of):
            # 在评测开始前快速失败。
            raise ValueError("禁止事实不能包含空白表述")
        # 归一化后去重，避免同一错误说法重复计权。
        normalized_terms = [_normalize_text(term) for term in self.answer_any_of]
        # 数量变化表示原标签只是标点或空格不同。
        if len(normalized_terms) != len(set(normalized_terms)):
            # 要求只保留一个稳定表述。
            raise ValueError("禁止事实表述归一化后不能重复")
        # 返回完成校验的规则。
        return self


class GroundedAnswerSuccessCase(BaseModel):
    """一条私有盲测题及其事实级人工金标。"""

    # case_id是公开聚合结果唯一允许暴露的题目标识。
    case_id: str = Field(min_length=1, max_length=100)
    # question是唯一会发送给范围门、检索器和回答模型的字段。
    question: str = Field(min_length=1, max_length=500)
    # should_answer表示当前已发布公开语料是否足以给出可靠结论。
    should_answer: bool
    # expected_document_ids列出完整回答允许依赖的父文档集合。
    expected_document_ids: list[str] = Field(default_factory=list, max_length=5)
    # required_facts只在模型返回后用于本地评分，绝不会进入生成Prompt。
    required_facts: list[RequiredFactRule] = Field(default_factory=list, max_length=10)
    # forbidden_claims同样只在本地评分阶段读取。
    forbidden_claims: list[ForbiddenClaimRule] = Field(default_factory=list, max_length=10)
    # tags只帮助私下复盘题型，不进入模型或公开聚合报告。
    tags: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_case_labels(self) -> "GroundedAnswerSuccessCase":
        """保证可回答题和知识缺口题的标签没有互相矛盾。"""

        # 所有文档ID都按稳定顺序保存。
        if self.expected_document_ids != sorted(set(self.expected_document_ids)):
            # 重复或乱序会让文件哈希变化难以解释。
            raise ValueError("expected_document_ids必须去重并按升序排列")
        # 可回答题必须同时有正确来源和至少一个原子事实。
        if self.should_answer and (
            not self.expected_document_ids or not self.required_facts
        ):
            # 没有事实金标就退化回第29步的“引用ID合法”口径。
            raise ValueError("可回答题必须配置预期文档和关键事实")
        # 知识缺口题不能携带会暗示答案的金标。
        if not self.should_answer and (
            self.expected_document_ids or self.required_facts or self.forbidden_claims
        ):
            # 负例只验证最终是否拒答。
            raise ValueError("不可回答题不能配置文档、关键事实或禁止事实")
        # fact_id与claim_id在单题内必须唯一，避免失败报告歧义。
        fact_ids = [fact.fact_id for fact in self.required_facts]
        claim_ids = [claim.claim_id for claim in self.forbidden_claims]
        # 重复事实ID直接拒绝加载。
        if len(fact_ids) != len(set(fact_ids)):
            # 数据作者需给每条事实独立稳定ID。
            raise ValueError("同一题的fact_id不能重复")
        # 重复禁止结论ID同样会破坏诊断。
        if len(claim_ids) != len(set(claim_ids)):
            # 数据作者需修复重复ID。
            raise ValueError("同一题的claim_id不能重复")
        # 每个事实来源必须属于当前题允许的预期文档集合。
        expected_set = set(self.expected_document_ids)
        # 逐条检查多文档问题的事实归属。
        if any(
            not set(fact.supporting_document_ids).issubset(expected_set)
            for fact in self.required_facts
        ):
            # 禁止用未标注的相似文档支撑答案。
            raise ValueError("事实支持文档必须属于expected_document_ids")
        # 每个预期文档都必须真正支撑至少一条事实，不能为了提高命中率虚加来源。
        fact_source_ids = {
            document_id
            for fact in self.required_facts
            for document_id in fact.supporting_document_ids
        }
        # 正例要求事实来源集合和预期引用集合完全一致。
        if self.should_answer and fact_source_ids != expected_set:
            # 否则多文档引用门会要求模型引用一份与答案事实无关的文档。
            raise ValueError("expected_document_ids必须与全部事实支持文档完全一致")
        # 返回完成校验的盲测样本。
        return self


class GroundedAnswerSuccessExperimentConfig(BaseModel):
    """第34步语料、私有盲测摘要、候选装配与唯一质量门。"""

    # 三个版本字段共同标识实验与评分规则。
    experiment_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    evaluator_version: str = Field(min_length=1, max_length=50)
    # 语料路径和内容SHA共同防止同路径静默换政策。
    corpus_path: str = Field(min_length=1, max_length=500)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # 私有盲测路径只存在于本机，公开仓库只保存其SHA和计数。
    blind_dataset_path: str = Field(min_length=1, max_length=500)
    blind_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blind_case_count: int = Field(ge=10, le=1000)
    blind_answerable_count: int = Field(ge=1, le=1000)
    blind_unanswerable_count: int = Field(ge=1, le=1000)
    # 旧数据集只用于精确重复检测，不把其标签发送给模型。
    leakage_reference_paths: list[str] = Field(min_length=1, max_length=50)
    # 以下参数完整冻结切片、两路召回、RRF和证据阈值。
    chunk_size: int = Field(ge=100, le=2000)
    chunk_overlap: int = Field(ge=0, le=500)
    top_k: int = Field(ge=1, le=20)
    dense_k: int = Field(ge=1, le=50)
    lexical_k: int = Field(ge=1, le=50)
    rrf_k: int = Field(ge=1, le=500)
    dense_weight: float = Field(gt=0.0, le=10.0)
    lexical_weight: float = Field(gt=0.0, le=10.0)
    score_threshold: float = Field(ge=0.0, le=1.0)
    # 真实候选的Embedding参数也进入候选指纹。
    embedding_dimensions: int = Field(ge=64, le=2560)
    embedding_model: str = Field(min_length=1, max_length=100)
    embedding_batch_size: int = Field(ge=1, le=100)
    # 聊天模型、温度和证据预算会直接改变最终答案，必须与Embedding一起冻结。
    chat_model: str = Field(min_length=1, max_length=100)
    chat_temperature: float = Field(ge=0.0, le=2.0)
    max_context_chars: int = Field(ge=500, le=20_000)
    candidate_profile_id: str = Field(min_length=1, max_length=100)
    # 真实盲测前必须把候选指纹写死，不能看到答案后再改装配。
    frozen_candidate_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    # 唯一平均指标门槛，避免继续堆叠Recall、MRR等分数。
    min_grounded_answer_success_rate: float = Field(ge=0.0, le=1.0)
    # 三类严重错误使用否决权，不把它们包装成另一组平均指标。
    zero_tolerance_failure_codes: list[
        Literal[
            "forbidden_claim_present",
            "invalid_or_unsupported_citation",
            "unsupported_answer_generated",
        ]
    ] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_experiment_contract(self) -> "GroundedAnswerSuccessExperimentConfig":
        """校验样本计数、切片窗口、候选池和红线集合。"""

        # 正负例计数必须精确组成总数，防止运行前后偷偷改变类别权重。
        if self.blind_answerable_count + self.blind_unanswerable_count != self.blind_case_count:
            # 配置摘要与私有文件必须一致。
            raise ValueError("盲测正负例计数之和必须等于总数")
        # overlap覆盖整个窗口会让切片器无法向前推进。
        if self.chunk_overlap >= self.chunk_size:
            # 运行前立即拒绝非法窗口。
            raise ValueError("chunk_overlap必须小于chunk_size")
        # 两条独立召回通道都应至少能提供最终Top-K候选。
        if self.dense_k < self.top_k or self.lexical_k < self.top_k:
            # 否则所谓双路召回存在名存实亡的通道。
            raise ValueError("dense_k和lexical_k都不能小于top_k")
        # 红线失败码必须去重并按升序保存，保证配置指纹稳定。
        if self.zero_tolerance_failure_codes != sorted(
            set(self.zero_tolerance_failure_codes)
        ):
            # 当前公开配置必须使用稳定顺序。
            raise ValueError("zero_tolerance_failure_codes必须去重并按升序排列")
        # 返回完成校验的实验契约。
        return self


class GroundedAnswerSuccessCaseResult(BaseModel):
    """单题只公开通过状态和有限诊断，不泄漏问题、答案或事实金标。"""

    # case_id允许在私有文件中定位Bad Case。
    case_id: str
    # should_answer仅公开正负类别，不公开问题内容。
    should_answer: bool
    # terminal_stage说明请求停在范围、检索、证据判断还是回答层。
    terminal_stage: Literal[
        "scope_rejected",
        "retrieval_empty",
        "grounding_declined",
        "answered",
    ]
    # cited_document_ids只保存公开知识ID，不保存切片正文。
    cited_document_ids: list[str]
    # matched/missing/unsupported只使用稳定fact_id。
    matched_fact_ids: list[str]
    missing_fact_ids: list[str]
    unsupported_fact_ids: list[str]
    # forbidden_claim_ids只保存错误结论ID，不保存实际模型答案。
    forbidden_claim_ids: list[str]
    # passed是本题唯一得分：完整满足为一，否则为零。
    passed: bool
    # failure_codes使用有限、稳定的根因码。
    failure_codes: list[str]


class GroundedAnswerSuccessSummary(BaseModel):
    """一套Profile在私有盲测上的单一核心指标与逐题诊断。"""

    # profile_id区分零费用对照与真实千问冻结候选。
    profile_id: str
    # 样本计数让百分比有明确分母。
    total_cases: int = Field(ge=1)
    answerable_cases: int = Field(ge=1)
    unanswerable_cases: int = Field(ge=1)
    # passed_cases是严格全或无通过的题数。
    passed_cases: int = Field(ge=0)
    # grounded_answer_success_rate是本实验唯一对外主指标。
    grounded_answer_success_rate: float = Field(ge=0.0, le=1.0)
    # red_line_case_ids表示至少触发一类零容忍错误的题。
    red_line_case_ids: list[str]
    # quality_gate只由单一成功率门和红线否决共同决定。
    quality_gate_passed: bool
    quality_gate_failures: list[str]
    # grounding_chat_calls记录实际回答器调用数，供费用审计而非质量排名。
    grounding_chat_calls: int = Field(ge=0)
    # results保存不含问题与答案正文的逐题审计轨迹。
    results: list[GroundedAnswerSuccessCaseResult]


class GroundedAnswerSuccessExperimentReport(BaseModel):
    """第34步离线基线与可选真实候选的脱敏报告。"""

    # 实验身份、语料与盲测摘要证明本轮输入已经冻结。
    experiment_id: str
    experiment_version: str
    evaluator_version: str
    corpus_sha256: str
    blind_dataset_sha256: str
    blind_case_count: int
    # 候选身份和指纹防止揭晓后换参数。
    candidate_profile_id: str
    candidate_fingerprint: str
    embedding_model: str
    chat_model: str
    # 费用计划在任何外部调用前就能展示。
    planned_embedding_requests: int = Field(ge=1)
    planned_chat_calls: int = Field(ge=0)
    # paid_api_called严格区分默认零费用路径与真实千问路径。
    paid_api_called: bool
    actual_embedding_requests: int = Field(ge=0)
    actual_embedding_input_tokens: int = Field(ge=0)
    actual_chat_calls: int = Field(ge=0)
    # 未显式确认盲测时两个Summary都保持None，证明没有读取私有正文。
    offline_baseline: GroundedAnswerSuccessSummary | None = None
    qwen_candidate: GroundedAnswerSuccessSummary | None = None


class _BatchCachedEmbeddingClient:
    """批量缓存盲测查询向量，禁止逐题产生隐藏收费请求。"""

    def __init__(self, delegate: EmbeddingClient) -> None:
        """保存真实客户端并初始化空查询缓存。"""

        # delegate处理文档批量向量与真实API计数。
        self._delegate = delegate
        # query_vectors只在显式预加载时写入。
        self._query_vectors: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        """向Qdrant暴露底层固定向量维度。"""

        # 文档和问题必须位于同一向量空间。
        return self._delegate.dimension

    def preload_queries(self, questions: Sequence[str]) -> None:
        """按服务商批次一次性生成尚未缓存的问题向量。"""

        # 去重同时保持私有数据集的原始顺序。
        missing = [
            question
            for question in dict.fromkeys(questions)
            if question not in self._query_vectors
        ]
        # 空列表不会发出外部请求。
        if not missing:
            # 直接结束保持真实计数不变。
            return
        # embed_documents具有批处理能力且使用同一模型参数。
        vectors = self._delegate.embed_documents(missing)
        # strict zip保证响应数量错位时立即失败。
        for question, vector in zip(missing, vectors, strict=True):
            # 保存当前问题对应向量。
            self._query_vectors[question] = vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """建库阶段直接委托批量Embedding。"""

        # 文档只在隔离索引创建时向量化一次。
        return self._delegate.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """查询阶段只允许读取已付费缓存。"""

        # 缺失表示费用规划器有漏洞，不能临时补发请求。
        if text not in self._query_vectors:
            # 错误不包含Key或向量。
            raise ValueError("查询未预加载，禁止隐式Embedding调用")
        # 返回已缓存的查询向量。
        return self._query_vectors[text]


def _normalize_text(text: str) -> str:
    """统一全半角、大小写、空白与标点，保留中文、英文和数字。"""

    # NFKC把全角数字等兼容字符归一化，casefold统一英文大小写。
    compatible = unicodedata.normalize("NFKC", text).casefold()
    # 删除不影响事实含义的空白与标点，降低模型格式差异造成的假失败。
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", compatible)


def _contains_asserted_term(text: str, term: str) -> bool:
    """只在候选表述没有被附近否定词反转时返回True。

    例如“不能保证一定退款”虽然包含“一定退款”，但它表达的恰好是“不承诺”；
    同理，“不可以取消”不能因为包含“可以取消”就算作正向事实命中。
    """

    # 金标本身含顿号/逗号时要允许完整枚举跨过分隔符；普通短语则按分句判断否定范围。
    clauses = (
        [text]
        if re.search(r"[，,、]", term)
        else re.split(r"[。！？!?；;，,、\n\r]+", text)
    )
    # 这些词出现在命中位置前很短的范围内时，通常会反转后面的断言。
    negation_markers = (
        "并不是",
        "并非",
        "不是",
        "不能",
        "不会",
        "不可",
        "不应",
        "不代表",
        "没有",
        "无需",
        "无须",
        "无法",
        "未",
        "没",
        "不",
    )
    # 表述本身也使用与答案相同的全半角、大小写和标点归一化规则。
    normalized_term = _normalize_text(term)
    # Schema已经禁止空表述；此处继续防御直接内部调用。
    if not normalized_term:
        # 空表述绝不能匹配所有答案。
        return False
    # 逐分句查找同一表述的所有位置，后一个位置可能没有被否定。
    for clause in clauses:
        # 删除当前分句内部不影响含义的格式字符。
        normalized_clause = _normalize_text(clause)
        # 从分句开头寻找第一次出现。
        start = normalized_clause.find(normalized_term)
        # find返回-1表示当前分句没有该表述。
        while start >= 0:
            # 只观察命中前八个归一化字符，覆盖“并非由”“不能保证”等常见结构。
            prefix_tail = normalized_clause[max(0, start - 8) : start]
            # 附近没有否定标记时，该表述才算真正被答案断言。
            if not any(marker in prefix_tail for marker in negation_markers):
                # 找到一个未被否定的实例即可通过当前OR候选。
                return True
            # 从当前命中后一位继续查找，允许同一句先否定后重新肯定。
            start = normalized_clause.find(normalized_term, start + 1)
    # 所有实例都不存在或都被否定时返回False。
    return False


def _is_negation_operator_group(group: Sequence[str]) -> bool:
    """判断一组候选是否专门表达“不允许/不成立”等否定关系。"""

    # 条件“未进入制作”不是否定答案结论；因此要求整组都像较短的否定谓词。
    operator_prefixes = (
        "通常不",
        "一般不",
        "并不是",
        "并非",
        "不是",
        "不能",
        "不会",
        "不可",
        "不应",
        "不代表",
        "不等于",
        "不属于",
        "不得",
        "无法",
        "没有",
        "不",
    )
    # 所有同义候选都必须是短否定谓词，避免把普通前置条件误当成逻辑操作符。
    return all(
        len(_normalize_text(term)) <= 6
        and _normalize_text(term).startswith(operator_prefixes)
        for term in group
    )


def _contains_term_governed_by_operator(
    text: str,
    term: str,
    operators: Sequence[str],
) -> bool:
    """检查目标表述是否正好被本条事实要求的否定谓词支配。"""

    # 与断言检查使用相同分句边界，禁止跨句借用“不”。
    clauses = re.split(r"[。！？!?；;，,、\n\r]+", text)
    # 预先归一化已在答案中真实命中的否定谓词。
    normalized_operators = [_normalize_text(operator) for operator in operators]
    # 目标表述按同一规则归一化。
    normalized_term = _normalize_text(term)
    # 逐分句寻找“不能 + 只凭名称”“不代表 + 一定免费”等结构。
    for clause in clauses:
        # 分句内部移除格式差异。
        normalized_clause = _normalize_text(clause)
        # 找到当前目标表述的位置。
        start = normalized_clause.find(normalized_term)
        # 同一分句可能出现多次目标表述。
        while start >= 0:
            # 与_contains_asserted_term保持相同八字符否定窗口。
            prefix_tail = normalized_clause[max(0, start - 8) : start]
            # 只有本条事实自己的否定谓词出现在窗口中才允许该否定关系通过。
            if any(operator in prefix_tail for operator in normalized_operators):
                # 当前目标正是被期望的否定谓词支配。
                return True
            # 继续寻找下一个实例。
            start = normalized_clause.find(normalized_term, start + 1)
    # 没有发现期望的否定关系。
    return False


def _matches_all_groups(text: str, groups: Sequence[Sequence[str]]) -> bool:
    """每个AND组至少匹配一个OR候选时返回True。"""

    # 先找出本条事实中明确要求、并且答案确实表达的否定谓词。
    matched_operators = [
        term
        for group in groups
        if _is_negation_operator_group(group)
        for term in group
        if _contains_asserted_term(text, term)
    ]
    # 每个AND组都必须找到一个同义候选。
    for group in groups:
        # 普通未被否定的断言可以直接命中。
        if any(_contains_asserted_term(text, term) for term in group):
            # 当前组满足，继续检查下一组。
            continue
        # “不能只凭名称”中“只凭名称”虽被否定，但这正是事实要求的关系。
        if matched_operators and any(
            _contains_term_governed_by_operator(text, term, matched_operators)
            for term in group
        ):
            # 当前对象组由已匹配的期望否定谓词支配，也算满足。
            continue
        # 既没有正向断言，也没有期望的否定关系，整条事实失败。
        return False
    # 所有组都满足时才返回True。
    return True


def _sha256_file(path: Path) -> str:
    """返回文件原始字节的SHA-256十六进制摘要。"""

    # read_bytes保留换行和编码差异，使任何文件修改都会改变摘要。
    return sha256(path.read_bytes()).hexdigest()


def load_grounded_answer_success_config(
    path: Path,
) -> GroundedAnswerSuccessExperimentConfig:
    """读取并校验第34步公开实验契约。"""

    # 配置不含私有题目正文，可以安全进入Git与CI。
    return GroundedAnswerSuccessExperimentConfig.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _collect_reference_questions(value: object) -> list[str]:
    """递归收集旧JSON中的question字段，用于精确重复检测。"""

    # 字典可能是一条Case或包裹Cases的配置对象。
    if isinstance(value, dict):
        # 当前层若有字符串问题就先收集。
        current = [value["question"]] if isinstance(value.get("question"), str) else []
        # 再递归扫描所有子值，兼容不同旧数据格式。
        return current + [
            question
            for child in value.values()
            for question in _collect_reference_questions(child)
        ]
    # 列表逐项递归。
    if isinstance(value, list):
        # 展平每个元素中找到的问题。
        return [
            question
            for child in value
            for question in _collect_reference_questions(child)
        ]
    # 其他标量不包含问题字段。
    return []


def load_private_grounded_answer_cases(
    config: GroundedAnswerSuccessExperimentConfig,
    *,
    confirm_blind: bool,
) -> list[GroundedAnswerSuccessCase]:
    """显式确认后读取、验Hash并执行旧题精确去重。"""

    # 没有确认时禁止解析路径，测试可据此证明默认流程不接触私有正文。
    if not confirm_blind:
        # 固定错误不包含私有路径。
        raise ValueError("读取私有盲测集前必须显式确认confirm_blind")
    # 私有路径由项目根统一解析。
    blind_path = resolve_project_path(config.blind_dataset_path)
    # 文件缺失时说明当前机器尚未安全放入sealed数据。
    if not blind_path.is_file():
        # 只显示公开配置中的相对路径，不打印用户目录。
        raise FileNotFoundError("本机缺少第34步私有盲测文件")
    # 原始字节摘要必须与揭晓前公开冻结值完全一致。
    if _sha256_file(blind_path) != config.blind_dataset_sha256:
        # 禁止通过改题、删题或改标签提高分数。
        raise ValueError("私有盲测文件SHA-256与冻结配置不一致")
    # 强类型解析每条事实规则。
    cases = TypeAdapter(list[GroundedAnswerSuccessCase]).validate_json(
        blind_path.read_text(encoding="utf-8")
    )
    # 空集、单侧集和计数变化都不能产生合法盲测结果。
    answerable_count = sum(1 for case in cases if case.should_answer)
    # 负例数量由总数减正例得到。
    unanswerable_count = len(cases) - answerable_count
    # 同时比对三个冻结计数。
    if (
        len(cases) != config.blind_case_count
        or answerable_count != config.blind_answerable_count
        or unanswerable_count != config.blind_unanswerable_count
    ):
        # 不输出题目，只指出摘要变化。
        raise ValueError("私有盲测实际正负例计数与冻结配置不一致")
    # case_id和问题都必须唯一，避免重复题人为放大结果稳定性。
    case_ids = [case.case_id for case in cases]
    # 归一化问题能发现只改空格或标点的重复。
    normalized_questions = [_normalize_text(case.question) for case in cases]
    # 重复ID会破坏Bad Case定位。
    if len(case_ids) != len(set(case_ids)):
        # 要求私有数据作者修复ID。
        raise ValueError("私有盲测case_id不能重复")
    # 重复问题不能被重复计权。
    if len(normalized_questions) != len(set(normalized_questions)):
        # 不打印具体问题以避免泄漏。
        raise ValueError("私有盲测问题归一化后不能重复")
    # old_questions汇总所有公开开发集与已揭晓holdout中的问题。
    old_questions: set[str] = set()
    # 逐个读取配置中公开声明的参考数据集。
    for reference_path_value in config.leakage_reference_paths:
        # 参考路径同样固定从项目根解析。
        reference_path = resolve_project_path(reference_path_value)
        # 旧数据缺失会削弱泄漏检查，因此不能静默跳过。
        if not reference_path.is_file():
            # 错误只显示公开相对路径。
            raise FileNotFoundError(f"泄漏参考数据集不存在：{reference_path_value}")
        # JSON结构可能不同，递归收集question字段。
        reference_value = json.loads(reference_path.read_text(encoding="utf-8"))
        # 归一化后加入集合。
        old_questions.update(
            _normalize_text(question)
            for question in _collect_reference_questions(reference_value)
        )
    # 精确重复意味着这不是未知题，整轮实验必须停止。
    if any(question in old_questions for question in normalized_questions):
        # 不打印碰撞正文，避免盲测泄漏到日志。
        raise ValueError("私有盲测包含已公开或已揭晓问题")
    # 返回经过SHA、Schema、计数和去重四重校验的样本。
    return cases


def grounded_answer_candidate_fingerprint(
    config: GroundedAnswerSuccessExperimentConfig,
) -> str:
    """计算不包含盲测金标、但覆盖完整候选行为的SHA-256。"""

    # 指纹使用实际语料内容摘要，不只使用可能静默换内容的路径。
    payload = {
        "experiment_version": config.version,
        "evaluator_version": config.evaluator_version,
        "corpus_sha256": config.corpus_sha256,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        "top_k": config.top_k,
        "dense_k": config.dense_k,
        "lexical_k": config.lexical_k,
        "rrf_k": config.rrf_k,
        "dense_weight": config.dense_weight,
        "lexical_weight": config.lexical_weight,
        "score_threshold": config.score_threshold,
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
        "chat_model": config.chat_model,
        "chat_temperature": config.chat_temperature,
        "max_context_chars": config.max_context_chars,
        "grounding_prompt": GROUNDED_ANSWER_SYSTEM_PROMPT,
        "query_policy": "deterministic_v1",
        "candidate_profile_id": config.candidate_profile_id,
    }
    # sort_keys和紧凑分隔符保证不同机器得到相同字节序列。
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # UTF-8摘要是公开候选冻结值。
    return sha256(serialized.encode("utf-8")).hexdigest()


def _verify_corpus(config: GroundedAnswerSuccessExperimentConfig) -> Path:
    """验证受治理语料原始字节与冻结摘要一致。"""

    # 语料路径固定从项目根解析。
    corpus_path = resolve_project_path(config.corpus_path)
    # 缺失语料无法建立可审计索引。
    if not corpus_path.is_file():
        # 公开配置中的相对路径足以定位问题。
        raise FileNotFoundError("第34步冻结语料不存在")
    # 任何正文、状态、版本或换行变化都会触发新实验版本。
    if _sha256_file(corpus_path) != config.corpus_sha256:
        # 禁止在同一实验版本下静默换知识。
        raise ValueError("知识语料SHA-256与第34步冻结配置不一致")
    # 返回已验证路径供仓库读取。
    return corpus_path


def validate_grounded_answer_evidence_labels(
    config: GroundedAnswerSuccessExperimentConfig,
    cases: Sequence[GroundedAnswerSuccessCase],
) -> None:
    """在揭晓前确认每条事实的证据锚点至少能命中一个实际切片。"""

    # 使用已验证SHA的同一份语料，避免拿另一版政策检查金标。
    corpus_path = _verify_corpus(config)
    # 仓库过滤规则与真实建库路径一致，只保留公开且已发布文档。
    documents = JsonKnowledgeRepository(corpus_path).list_indexable_documents()
    # 按冻结的500/80等窗口真正切片，不能只在整篇文档上搜索锚点。
    chunks = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    ).split_documents(documents)
    # failures只记录稳定Case/Fact ID，不输出问题和事实正文。
    failures: list[str] = []
    # 逐题检查每条原子事实。
    for case in cases:
        # 负例没有事实规则，循环自然跳过。
        for fact in case.required_facts:
            # 至少一个允许来源的真实切片必须完整覆盖证据锚点。
            supported = any(
                chunk.document_id in fact.supporting_document_ids
                and _matches_all_groups(chunk.content, fact.evidence_all_of)
                for chunk in chunks
            )
            # 拼接稳定ID便于私下修复标签，不泄漏金标内容。
            if not supported:
                # 同一字符串足以定位私有文件中的规则。
                failures.append(f"{case.case_id}:{fact.fact_id}")
    # 任一锚点拼写错误或跨切片无法支持，都必须在调用模型前失败。
    if failures:
        # 仅显示稳定ID，不打印私有问题、答案或证据正文。
        raise ValueError("盲测事实证据锚点无法被实际切片支持：" + ",".join(failures))


def _build_hybrid_retriever(
    config: GroundedAnswerSuccessExperimentConfig,
    *,
    embedding_client: EmbeddingClient,
    collection_name: str,
) -> tuple[KnowledgeRetriever, int]:
    """建立隔离Qdrant、全库BM25和冻结RRF的完整混合召回器。"""

    # 先验证语料字节，避免用变更后的知识得到不可复现结果。
    corpus_path = _verify_corpus(config)
    # 仓库只返回published且public文档。
    documents = JsonKnowledgeRepository(corpus_path).list_indexable_documents()
    # 切片器使用冻结窗口。
    chunker = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    )
    # chunks同时供Qdrant建库计数和全库BM25使用。
    chunks = chunker.split_documents(documents)
    # 每轮实验使用独立内存Qdrant，绝不污染Docker生产Collection。
    dense_retriever = QdrantKnowledgeRetriever(
        client=create_qdrant_client(":memory:"),
        collection_name=collection_name,
        embedding_client=embedding_client,
        score_threshold=config.score_threshold,
    )
    # 写入全部受治理公开切片。
    dense_retriever.ensure_index(documents, chunker=chunker)
    # 词面通道独立读取完整切片，不依赖向量候选。
    lexical_retriever = BM25CorpusRetriever(chunks=chunks)
    # RRF只按两路名次融合，不混加不同量纲原分数。
    hybrid_retriever = ReciprocalRankFusionRetriever(
        dense_retriever=dense_retriever,
        lexical_retriever=lexical_retriever,
        dense_k=config.dense_k,
        lexical_k=config.lexical_k,
        rrf_k=config.rrf_k,
        dense_weight=config.dense_weight,
        lexical_weight=config.lexical_weight,
    )
    # 返回统一检索协议和文档Embedding批次计算所需切片数。
    return hybrid_retriever, len(chunks)


def _allowed_questions(
    cases: Sequence[GroundedAnswerSuccessCase],
    policy: KnowledgeQueryPolicy,
) -> list[str]:
    """只返回范围门允许发送给外部Embedding服务的问题。"""

    # 被敏感规则拒绝的问题不应离开本地边界。
    return [case.question for case in cases if policy.assess(case.question).allowed]


async def evaluate_grounded_answer_success(
    *,
    profile_id: str,
    cases: Sequence[GroundedAnswerSuccessCase],
    query_policy: KnowledgeQueryPolicy,
    retriever: KnowledgeRetriever,
    answer_client: GroundedAnswerClient,
    top_k: int,
    min_success_rate: float,
    zero_tolerance_failure_codes: Sequence[str],
) -> GroundedAnswerSuccessSummary:
    """执行真实RAG链路，并按事实、证据和拒答给每题严格打零或一。"""

    # 空集和单侧集会让单一比例失去业务含义。
    if not cases:
        # 调用方必须提供非空样本。
        raise ValueError("有据回答成功率评测集不能为空")
    # 正例和负例都必须存在，防止全部拒答或全部回答获得虚高分。
    if not any(case.should_answer for case in cases) or not any(
        not case.should_answer for case in cases
    ):
        # 在模型调用前快速失败。
        raise ValueError("有据回答成功率评测必须同时包含正例与负例")
    # results按私有文件稳定顺序保存，但不含题目正文。
    results: list[GroundedAnswerSuccessCaseResult] = []
    # chat_calls只统计真正进入回答客户端的题。
    chat_calls = 0

    # 每题从范围判断开始，金标永远不会传给被评测系统。
    for case in cases:
        # assess只读取问题字符串。
        assessment = query_policy.assess(case.question)
        # 默认没有证据和回答，范围拒绝会保持该状态。
        hits: list[RetrievalHit] = []
        # 默认草稿表示最终安全拒答。
        draft = GroundedAnswerDraft(
            answer="当前证据不足，无法生成可靠回答。",
            citation_ids=[],
            is_answerable=False,
        )
        # 默认结束在范围门；后续真实执行会覆盖。
        terminal_stage: Literal[
            "scope_rejected",
            "retrieval_empty",
            "grounding_declined",
            "answered",
        ] = "scope_rejected"
        # 只有范围允许才进入检索。
        if assessment.allowed:
            # retriever只接收问题和Top-K，不接收事实规则或正确文档ID。
            hits = retriever.search(case.question, top_k=top_k)
            # 空证据直接安全结束。
            if not hits:
                # 记录检索为空。
                terminal_stage = "retrieval_empty"
            else:
                # 回答器同样只接收问题和检索证据，防止标签泄漏。
                draft = await answer_client.generate(
                    question=case.question,
                    evidence=hits,
                )
                # 每次真实调用只累加一次。
                chat_calls += 1
                # 结构化is_answerable决定是否面向用户回答。
                terminal_stage = (
                    "answered" if draft.is_answerable else "grounding_declined"
                )

        # 候选chunk白名单用于确定性引用校验。
        hit_by_chunk_id = {hit.chunk.chunk_id: hit for hit in hits}
        # 模型引用去重并保持首次顺序。
        citation_ids = list(dict.fromkeys(draft.citation_ids))
        # 自动回答必须至少引用一条，且所有ID都来自本轮候选。
        citation_ids_valid = bool(citation_ids) and set(citation_ids).issubset(
            hit_by_chunk_id
        )
        # 只从合法候选解析父文档，越界ID不会触发KeyError。
        cited_document_ids = list(
            dict.fromkeys(
                hit_by_chunk_id[citation_id].chunk.document_id
                for citation_id in citation_ids
                if citation_id in hit_by_chunk_id
            )
        )
        # cited_hits是唯一允许用于事实支持判断的证据集合。
        cited_hits = [
            hit_by_chunk_id[citation_id]
            for citation_id in citation_ids
            if citation_id in hit_by_chunk_id
        ]
        # 以下列表只保存稳定事实ID和失败码，不保存答案正文。
        matched_fact_ids: list[str] = []
        missing_fact_ids: list[str] = []
        unsupported_fact_ids: list[str] = []
        forbidden_claim_ids: list[str] = []
        failure_codes: list[str] = []

        # 禁止结论只在最终自动回答时检查，拒答草稿不会暴露给用户。
        if draft.is_answerable:
            # 任一完整错误表述命中就记录对应claim_id。
            forbidden_claim_ids = [
                claim.claim_id
                for claim in case.forbidden_claims
                if any(_contains_asserted_term(draft.answer, term) for term in claim.answer_any_of)
            ]

        # 可回答题执行事实与引用的全部硬条件。
        if case.should_answer:
            # 范围误拒、检索为空或模型拒答分别保留最接近根因的代码。
            if not assessment.allowed:
                # 正常售后题不应被前置范围门拒绝。
                failure_codes.append("scope_false_rejection")
            elif not hits:
                # 没有候选就不可能形成有据回答。
                failure_codes.append("retrieval_empty")
            elif not draft.is_answerable:
                # 有足够人工金标时模型错误拒答。
                failure_codes.append("answerable_case_declined")
            # 只有自动回答才检查引用和事实。
            if draft.is_answerable:
                # 多文档题要求所有人工标注来源都被实际引用。
                expected_documents_cited = set(case.expected_document_ids).issubset(
                    cited_document_ids
                )
                # 合法ID与完整来源覆盖共同组成引用前置条件。
                if not citation_ids_valid or not expected_documents_cited:
                    # 该代码属于零容忍红线。
                    failure_codes.append("invalid_or_unsupported_citation")
                # 每个原子事实独立检查答案表达和实际引用证据。
                for fact in case.required_facts:
                    # 答案必须满足该事实的全部关系组。
                    answer_matches = _matches_all_groups(
                        draft.answer,
                        fact.answer_all_of,
                    )
                    # 只允许该事实人工标注来源中的实际引用切片提供支持。
                    supporting_evidence = "\n".join(
                        hit.chunk.content
                        for hit in cited_hits
                        if hit.chunk.document_id in fact.supporting_document_ids
                    )
                    # 引用正文必须满足证据锚点全部关系组。
                    evidence_matches = bool(supporting_evidence) and _matches_all_groups(
                        supporting_evidence,
                        fact.evidence_all_of,
                    )
                    # 答案没有表达当前关键事实时记录缺失。
                    if not answer_matches:
                        # 只保存fact_id。
                        missing_fact_ids.append(fact.fact_id)
                    # 答案表达了事实但引用切片无法支持时记录无依据。
                    elif not evidence_matches:
                        # 该题不能靠引用同文档的无关切片通过。
                        unsupported_fact_ids.append(fact.fact_id)
                    else:
                        # 答案与引用同时通过才算事实命中。
                        matched_fact_ids.append(fact.fact_id)
                # 任一事实缺失使本题失败。
                if missing_fact_ids:
                    # 聚合原因不泄漏具体表述。
                    failure_codes.append("required_fact_missing")
                # 任一事实没有被实际引用切片支持形成引用红线。
                if unsupported_fact_ids:
                    # 复用统一红线代码。
                    failure_codes.append("invalid_or_unsupported_citation")
                # 命中明确错误结论属于事实幻觉红线。
                if forbidden_claim_ids:
                    # 不允许平均分把严重错答稀释掉。
                    failure_codes.append("forbidden_claim_present")
            # 正例只有没有任何失败原因才得一分。
            passed = not failure_codes
        else:
            # 知识缺口最终自动回答就是无依据回答红线。
            if draft.is_answerable:
                # 不论答案看起来多流畅都记为失败。
                failure_codes.append("unsupported_answer_generated")
            # 拒答草稿仍携带引用说明结构化输出不干净，也不算完整通过。
            elif citation_ids:
                # 该情况不是对外幻觉红线，但需要修复回答Schema遵循。
                failure_codes.append("abstention_returned_citations")
            # 负例只有拒答且没有引用才得一分。
            passed = not failure_codes

        # 逐题结果不保存question、answer、规则正文或隐藏推理。
        results.append(
            GroundedAnswerSuccessCaseResult(
                case_id=case.case_id,
                should_answer=case.should_answer,
                terminal_stage=terminal_stage,
                cited_document_ids=cited_document_ids,
                matched_fact_ids=matched_fact_ids,
                missing_fact_ids=missing_fact_ids,
                unsupported_fact_ids=unsupported_fact_ids,
                forbidden_claim_ids=forbidden_claim_ids,
                passed=passed,
                failure_codes=list(dict.fromkeys(failure_codes)),
            )
        )

    # 严格通过数是唯一主指标分子。
    passed_cases = sum(1 for result in results if result.passed)
    # 总题数是固定分母，每题权重相同且不打部分分。
    success_rate = passed_cases / len(results)
    # 红线集合来自公开配置而不是运行后临时选择。
    zero_tolerance_set = set(zero_tolerance_failure_codes)
    # 只保存触发任一红线的case_id。
    red_line_case_ids = [
        result.case_id
        for result in results
        if zero_tolerance_set.intersection(result.failure_codes)
    ]
    # quality_gate_failures只有单一比例门和红线否决两种契约原因。
    gate_failures: list[str] = []
    # 成功率低于揭晓前冻结门槛时失败。
    if success_rate < min_success_rate:
        # 固定原因码便于CI和报告聚合。
        gate_failures.append("grounded_answer_success_rate_below_threshold")
    # 任一严重错答、禁止事实或非法引用直接否决。
    if red_line_case_ids:
        # 这是安全不变量，不是第二个用于优化的平均指标。
        gate_failures.append("zero_tolerance_failure_detected")
    # 返回单一headline和逐题脱敏诊断。
    return GroundedAnswerSuccessSummary(
        profile_id=profile_id,
        total_cases=len(results),
        answerable_cases=sum(1 for case in cases if case.should_answer),
        unanswerable_cases=sum(1 for case in cases if not case.should_answer),
        passed_cases=passed_cases,
        grounded_answer_success_rate=success_rate,
        red_line_case_ids=red_line_case_ids,
        quality_gate_passed=not gate_failures,
        quality_gate_failures=gate_failures,
        grounding_chat_calls=chat_calls,
        results=results,
    )


async def run_grounded_answer_success_experiment(
    config: GroundedAnswerSuccessExperimentConfig,
    *,
    runtime_settings: Settings,
    confirm_blind: bool = False,
    confirm_paid_api: bool = False,
) -> GroundedAnswerSuccessExperimentReport:
    """默认只展示费用计划；确认盲测后运行离线基线，可选再运行真实千问。"""

    # 付费调用没有盲测确认时没有合法输入，必须在任何外部访问前拒绝。
    if confirm_paid_api and not confirm_blind:
        # 固定错误不包含密钥或私有路径。
        raise ValueError("--confirm-paid-api必须同时确认私有盲测")
    # 候选指纹在读取盲测正文前即可计算和公开冻结。
    candidate_fingerprint = grounded_answer_candidate_fingerprint(config)
    # 语料摘要和切片数量用于费用计划，不涉及私有题目。
    corpus_path = _verify_corpus(config)
    # 读取治理后公开文档。
    documents = JsonKnowledgeRepository(corpus_path).list_indexable_documents()
    # 使用冻结窗口计算文档Embedding条数。
    chunk_count = len(
        KnowledgeChunker(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ).split_documents(documents)
    )
    # 文档批次加最坏情况下全部盲题查询批次。
    planned_embedding_requests = ceil(
        chunk_count / config.embedding_batch_size
    ) + ceil(config.blind_case_count / config.embedding_batch_size)
    # 每题最多进入一次结构化回答调用。
    planned_chat_calls = config.blind_case_count
    # 未确认时不读取私有文件、不建索引也不访问模型。
    if not confirm_blind:
        # 返回只含公开计划的报告。
        return GroundedAnswerSuccessExperimentReport(
            experiment_id=config.experiment_id,
            experiment_version=config.version,
            evaluator_version=config.evaluator_version,
            corpus_sha256=config.corpus_sha256,
            blind_dataset_sha256=config.blind_dataset_sha256,
            blind_case_count=config.blind_case_count,
            candidate_profile_id=config.candidate_profile_id,
            candidate_fingerprint=candidate_fingerprint,
            embedding_model=config.embedding_model,
            chat_model=config.chat_model,
            planned_embedding_requests=planned_embedding_requests,
            planned_chat_calls=planned_chat_calls,
            paid_api_called=False,
            actual_embedding_requests=0,
            actual_embedding_input_tokens=0,
            actual_chat_calls=0,
        )
    # 只有显式确认后才读取并校验私有题目。
    cases = load_private_grounded_answer_cases(config, confirm_blind=True)
    # 在任何Embedding或回答调用前排除错误金标造成的候选假失败。
    validate_grounded_answer_evidence_labels(config, cases)
    # 两套Profile共享同一范围门，避免比较时改变前置边界。
    query_policy = DeterministicFAQScopePolicy()
    # 建立零费用Hash+完整BM25/RRF基线。
    offline_retriever, _ = _build_hybrid_retriever(
        config,
        embedding_client=HashEmbeddingClient(config.embedding_dimensions),
        collection_name="serviceops-grounded-success-offline",
    )
    # Extractive基线会暴露“有相似证据就整段回答”的真实风险。
    offline_baseline = await evaluate_grounded_answer_success(
        profile_id="hash-hybrid-extractive-baseline",
        cases=cases,
        query_policy=query_policy,
        retriever=offline_retriever,
        answer_client=ExtractiveGroundedAnswerClient(),
        top_k=config.top_k,
        min_success_rate=config.min_grounded_answer_success_rate,
        zero_tolerance_failure_codes=config.zero_tolerance_failure_codes,
    )
    # 仅做零费用首轮时直接返回，不读取Key。
    if not confirm_paid_api:
        # candidate保持None，明确没有真实模型结论。
        return GroundedAnswerSuccessExperimentReport(
            experiment_id=config.experiment_id,
            experiment_version=config.version,
            evaluator_version=config.evaluator_version,
            corpus_sha256=config.corpus_sha256,
            blind_dataset_sha256=config.blind_dataset_sha256,
            blind_case_count=config.blind_case_count,
            candidate_profile_id=config.candidate_profile_id,
            candidate_fingerprint=candidate_fingerprint,
            embedding_model=config.embedding_model,
            chat_model=config.chat_model,
            planned_embedding_requests=planned_embedding_requests,
            planned_chat_calls=planned_chat_calls,
            paid_api_called=False,
            actual_embedding_requests=0,
            actual_embedding_input_tokens=0,
            actual_chat_calls=0,
            offline_baseline=offline_baseline,
        )
    # 真实调用前必须确认候选指纹与公开配置冻结值完全一致。
    if config.frozen_candidate_fingerprint != candidate_fingerprint:
        # 禁止揭晓后换参数或未冻结就付费运行。
        raise ValueError("第34步真实候选尚未冻结或候选指纹已变化")
    # SecretStr只在创建SDK客户端时短暂解包。
    api_key = (
        runtime_settings.llm_api_key.get_secret_value()
        if runtime_settings.llm_api_key is not None
        else ""
    )
    # 缺Key时在外部调用前快速失败。
    if not api_key:
        # 不回显环境变量值。
        raise ValueError("第34步真实候选需要SERVICEOPS_LLM_API_KEY")
    # OpenAI兼容地址必须与Key地域匹配。
    if not runtime_settings.llm_base_url:
        # 只指出缺失配置。
        raise ValueError("第34步真实候选需要SERVICEOPS_LLM_BASE_URL")
    # 创建可计数的真实千问Embedding客户端。
    raw_embedding_client = OpenAICompatibleEmbeddingClient(
        api_key=api_key,
        base_url=runtime_settings.llm_base_url,
        model=config.embedding_model,
        dimension=config.embedding_dimensions,
        batch_size=config.embedding_batch_size,
        timeout_seconds=runtime_settings.llm_timeout_seconds,
        max_retries=runtime_settings.llm_max_retries,
    )
    # 缓存包装器负责费用受控的批量查询向量。
    cached_embedding_client = _BatchCachedEmbeddingClient(raw_embedding_client)
    # 文档Embedding在隔离Qdrant建库时只发生一次。
    candidate_retriever, _ = _build_hybrid_retriever(
        config,
        embedding_client=cached_embedding_client,
        collection_name="serviceops-grounded-success-qwen",
    )
    # 只预加载范围门允许离开本地的问题。
    cached_embedding_client.preload_queries(_allowed_questions(cases, query_policy))
    # 回答后端强制切换到真实结构化Grounded模式，其他设置沿用.env。
    candidate_settings = runtime_settings.model_copy(
        update={
            "llm_backend": "openai_compatible",
            "llm_model": config.chat_model,
            "llm_temperature": config.chat_temperature,
            "rag_generation_backend": "llm",
            "rag_max_context_chars": config.max_context_chars,
        }
    )
    # 工厂绑定qwen-plus和GroundedAnswerDraft Schema。
    candidate_answer_client = create_grounded_answer_client(candidate_settings)
    # 用完全相同的私有题、范围门、RRF和评分规则运行冻结候选。
    qwen_candidate = await evaluate_grounded_answer_success(
        profile_id=config.candidate_profile_id,
        cases=cases,
        query_policy=query_policy,
        retriever=candidate_retriever,
        answer_client=candidate_answer_client,
        top_k=config.top_k,
        min_success_rate=config.min_grounded_answer_success_rate,
        zero_tolerance_failure_codes=config.zero_tolerance_failure_codes,
    )
    # 返回真实调用计数和脱敏逐题结果。
    return GroundedAnswerSuccessExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        evaluator_version=config.evaluator_version,
        corpus_sha256=config.corpus_sha256,
        blind_dataset_sha256=config.blind_dataset_sha256,
        blind_case_count=config.blind_case_count,
        candidate_profile_id=config.candidate_profile_id,
        candidate_fingerprint=candidate_fingerprint,
        embedding_model=config.embedding_model,
        chat_model=candidate_settings.llm_model,
        planned_embedding_requests=planned_embedding_requests,
        planned_chat_calls=planned_chat_calls,
        paid_api_called=True,
        actual_embedding_requests=raw_embedding_client.api_request_count,
        actual_embedding_input_tokens=raw_embedding_client.input_token_count,
        actual_chat_calls=qwen_candidate.grounding_chat_calls,
        offline_baseline=offline_baseline,
        qwen_candidate=qwen_candidate,
    )
