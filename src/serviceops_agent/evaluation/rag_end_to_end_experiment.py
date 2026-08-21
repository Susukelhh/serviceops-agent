"""第29步：把范围门、检索、重排、证据充分性和引用校验串成端到端RAG实验。"""

# Sequence允许评测器接收列表或其他只读样本序列。
# json生成稳定候选指纹；Path读取版本化JSON文件。
import json
from collections.abc import Sequence

# sha256冻结完整候选配置，防止开发通过后悄悄修改某一层参数。
from hashlib import sha256

# ceil用于按服务商批次上限估算Embedding请求数。
from math import ceil
from pathlib import Path

# Literal限制报告阶段和Profile名称，TypeAdapter校验顶层JSON数组。
from typing import Literal

# Pydantic为配置、样本、逐题结果和汇总报告提供运行时校验。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# 项目路径解析不依赖PyCharm当前工作目录。
from serviceops_agent.config.paths import resolve_project_path

# Settings提供千问密钥、地址、超时、重试和聊天模型名。
from serviceops_agent.config.settings import Settings

# GroundedAnswerDraft和RetrievalHit复用线上FAQ节点的领域边界。
from serviceops_agent.domain.knowledge import GroundedAnswerDraft, RetrievalHit

# 只加载已发布且允许公开索引的知识文档。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# 切片器沿用已经冻结的500字符窗口和80字符重叠。
from serviceops_agent.rag.chunking import KnowledgeChunker

# Hash是当前离线基线；真实客户端调用千问Embedding兼容接口。
from serviceops_agent.rag.embeddings import (
    EmbeddingClient,
    HashEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)

# 两种回答器分别代表旧“有证据就答”和新“先判断证据是否充分”。
from serviceops_agent.rag.generation import (
    GROUNDED_ANSWER_SYSTEM_PROMPT,
    ExtractiveGroundedAnswerClient,
    GroundedAnswerClient,
    create_grounded_answer_client,
)

# 确定性范围门在Embedding前拦截高置信域外和敏感请求。
from serviceops_agent.rag.query_policy import (
    DeterministicFAQScopePolicy,
    KnowledgeQueryPolicy,
)

# BM25只重排向量已经召回的固定候选，不凭空创建知识。
from serviceops_agent.rag.reranking import BM25CandidateReranker, RerankingKnowledgeRetriever

# Qdrant内存索引保证实验隔离，不污染本地生产演示索引。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    QdrantKnowledgeRetriever,
    create_qdrant_client,
)


class RAGEndToEndCase(BaseModel):
    """一条从原始用户问题开始、一直评到最终回答决策的人工标注样本。"""

    # case_id是报告和Bad Case回归使用的稳定标识。
    case_id: str = Field(min_length=1, max_length=100)
    # question是未经改写、直接进入范围门的用户问题。
    question: str = Field(min_length=1, max_length=500)
    # expected_document_ids列出真正能够回答该问题的父文档ID。
    expected_document_ids: list[str] = Field(default_factory=list, max_length=5)
    # should_answer表示当前公开知识库是否足以自动回答。
    should_answer: bool
    # tags只用于按业务类型复盘，不参与模型或质量门决策。
    tags: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_answer_label(self) -> "RAGEndToEndCase":
        """确保可回答标签与预期文档集合没有互相矛盾。"""

        # 可回答题必须至少有一份人工标注的正确来源。
        if self.should_answer and not self.expected_document_ids:
            # 在实验启动前暴露标注错误。
            raise ValueError("should_answer=true时expected_document_ids不能为空")
        # 知识缺口题不能同时声称某份文档可以回答。
        if not self.should_answer and self.expected_document_ids:
            # 防止同一题被同时计作正例和负例。
            raise ValueError("should_answer=false时expected_document_ids必须为空")
        # 文档ID去重并保持排序，保证报告稳定可比较。
        if self.expected_document_ids != sorted(set(self.expected_document_ids)):
            # 数据作者需要显式整理标注。
            raise ValueError("expected_document_ids必须去重并按升序排列")
        # 返回通过组合校验的样本。
        return self


class RAGEndToEndQualityGate(BaseModel):
    """完整链路必须同时满足的检索、回答和安全指标。"""

    # min_retrieval_recall要求可回答题的正确文档进入最终Top-K。
    min_retrieval_recall: float = Field(ge=0.0, le=1.0)
    # min_top_1_accuracy衡量重排后的第一份文档是否正确。
    min_top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # min_answerable_recall防止系统通过全部转人工来伪造安全。
    min_answerable_recall: float = Field(ge=0.0, le=1.0)
    # min_abstention_accuracy要求知识缺口最终正确拒答。
    min_abstention_accuracy: float = Field(ge=0.0, le=1.0)
    # min_decision_accuracy综合所有回答和拒答结果。
    min_decision_accuracy: float = Field(ge=0.0, le=1.0)
    # max_unsupported_answer_rate限制没有答案时仍自动回答的比例。
    max_unsupported_answer_rate: float = Field(ge=0.0, le=1.0)
    # min_citation_validity要求所有放行答案只引用本轮候选白名单。
    min_citation_validity: float = Field(ge=0.0, le=1.0)


class RAGEndToEndExperimentConfig(BaseModel):
    """第29步开发、候选冻结和一次性锁定验收的版本化契约。"""

    # experiment_id与version共同定位本轮实验设计。
    experiment_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=50)
    # corpus_path指向治理后的困难知识语料。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_path允许观察和调试。
    development_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_dataset_path只在完整候选冻结后读取。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_case_count用于付费前估算，不需要提前加载锁定正文。
    holdout_case_count: int = Field(ge=1, le=1000)
    # chunk_size与chunk_overlap必须和知识标注版本一致。
    chunk_size: int = Field(ge=100, le=2000)
    chunk_overlap: int = Field(ge=0, le=500)
    # candidate_k是进入BM25的固定向量候选数量。
    candidate_k: int = Field(ge=1, le=20)
    # top_k是最终发送给回答器的最大证据数量。
    top_k: int = Field(ge=1, le=20)
    # baseline_embedding_dimensions记录当前Hash基线维度。
    baseline_embedding_dimensions: int = Field(ge=64, le=4096)
    # baseline_score_threshold记录当前离线基线阈值。
    baseline_score_threshold: float = Field(ge=0.0, le=1.0)
    # candidate_embedding_model是第27步接受端到端复验的语义模型。
    candidate_embedding_model: str = Field(min_length=1, max_length=100)
    # candidate_embedding_dimensions固定Qdrant向量空间大小。
    candidate_embedding_dimensions: int = Field(ge=64, le=4096)
    # candidate_embedding_batch_size服从服务商单批上限。
    candidate_embedding_batch_size: int = Field(ge=1, le=100)
    # candidate_score_threshold沿用第27步开发阶段冻结的0.50。
    candidate_score_threshold: float = Field(ge=0.0, le=1.0)
    # bm25_lexical_weight沿用第26步冻结的0.25融合权重。
    bm25_lexical_weight: float = Field(ge=0.0, le=1.0)
    # candidate_profile_id给整套组合候选一个稳定名称。
    candidate_profile_id: str = Field(min_length=1, max_length=100)
    # frozen_candidate_fingerprint开发通过后写入，控制holdout读取权限。
    frozen_candidate_fingerprint: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    # 开发与锁定集使用预先声明、不可事后放宽的质量门。
    development_gate: RAGEndToEndQualityGate
    holdout_gate: RAGEndToEndQualityGate

    @model_validator(mode="after")
    def validate_pipeline_parameters(self) -> "RAGEndToEndExperimentConfig":
        """校验切片、候选池和千问批次之间的组合关系。"""

        # overlap覆盖整个窗口会让切片无法向前移动。
        if self.chunk_overlap >= self.chunk_size:
            # 配置加载阶段快速失败。
            raise ValueError("chunk_overlap必须小于chunk_size")
        # 重排候选池不能小于最终需要的证据数。
        if self.candidate_k < self.top_k:
            # 否则不同Profile看到的候选规模不一致。
            raise ValueError("candidate_k不能小于top_k")
        # 当前qwen3.7同步Embedding接口单批最多20条。
        if (
            self.candidate_embedding_model == "qwen3.7-text-embedding"
            and self.candidate_embedding_batch_size > 20
        ):
            # 把服务商限制固定在实验版本中。
            raise ValueError("qwen3.7-text-embedding单批最多20条")
        # 返回通过组合校验的配置。
        return self


class RAGEndToEndCaseResult(BaseModel):
    """逐题保存每一层公开决策，便于定位错误属于哪一段。"""

    # case_id关联人工标注。
    case_id: str
    # should_answer保留正确的最终业务标签。
    should_answer: bool
    # scope_allowed表示问题是否通过确定性前置范围门。
    scope_allowed: bool
    # scope_reason_code说明范围门的有限、可审计原因。
    scope_reason_code: str
    # retrieved_document_ids保存重排后Top-K父文档顺序。
    retrieved_document_ids: list[str]
    # cited_document_ids保存模型最终实际引用的父文档顺序。
    cited_document_ids: list[str]
    # predicted_answerable是完整链路最终是否自动回答。
    predicted_answerable: bool
    # citation_valid表示引用至少一条且全部来自本轮候选。
    citation_valid: bool
    # terminal_stage表示请求在哪一层结束。
    terminal_stage: Literal["scope_rejected", "retrieval_empty", "grounding_declined", "answered"]
    # passed要求最终决策正确，正例还必须引用人工标注的正确文档。
    passed: bool
    # failure_codes使用稳定原因码，不保存隐藏推理或模型原始响应。
    failure_codes: list[str]


class RAGEndToEndSummary(BaseModel):
    """一套完整RAG Profile在一份数据集上的汇总指标。"""

    # profile_id与dataset说明比较对象。
    profile_id: str
    dataset: Literal["development", "holdout"]
    # 样本数量明确每项百分比的分母。
    total_cases: int = Field(ge=1)
    answerable_cases: int = Field(ge=1)
    unanswerable_cases: int = Field(ge=1)
    # retrieval_recall只看正例正确文档是否进入Top-K。
    retrieval_recall: float = Field(ge=0.0, le=1.0)
    # top_1_accuracy只看正例第一份文档是否正确。
    top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # answerable_recall要求正例最终回答且引用正确来源。
    answerable_recall: float = Field(ge=0.0, le=1.0)
    # abstention_accuracy衡量负例最终拒答比例。
    abstention_accuracy: float = Field(ge=0.0, le=1.0)
    # decision_accuracy综合全部最终决策。
    decision_accuracy: float = Field(ge=0.0, le=1.0)
    # unsupported_answer_rate直接衡量负例错误放行比例。
    unsupported_answer_rate: float = Field(ge=0.0, le=1.0)
    # citation_validity只在自动回答样本中计算白名单合法率。
    citation_validity: float = Field(ge=0.0, le=1.0)
    # grounding_chat_calls记录实际进入回答模型的题数。
    grounding_chat_calls: int = Field(ge=0)
    # quality_gate_passed与原因决定是否允许候选晋级。
    quality_gate_passed: bool
    quality_gate_failures: list[str]
    # results保存每条公开执行轨迹。
    results: list[RAGEndToEndCaseResult]


class RAGEndToEndExperimentReport(BaseModel):
    """当前离线链路与可选真实组合候选的完整对照报告。"""

    # 实验身份和候选指纹支持复现与冻结。
    experiment_id: str
    experiment_version: str
    candidate_profile_id: str
    candidate_fingerprint: str
    # 模型名分别记录真实语义向量和聊天回答器。
    embedding_model: str
    chat_model: str
    # planned字段让用户在付费前知道请求上界。
    planned_development_embedding_requests: int = Field(ge=1)
    planned_development_chat_calls: int = Field(ge=0)
    planned_holdout_extra_embedding_requests: int = Field(ge=1)
    planned_holdout_extra_chat_calls: int = Field(ge=0)
    # paid_api_called区分默认离线与真实候选。
    paid_api_called: bool
    # actual字段来自真实适配器计数和实际回答次数。
    actual_embedding_requests: int = Field(ge=0)
    actual_embedding_input_tokens: int = Field(ge=0)
    actual_chat_calls: int = Field(ge=0)
    # baseline_development始终存在，证明CI无需密钥也能回归。
    baseline_development: RAGEndToEndSummary
    # candidate_development只有显式付费确认后出现。
    candidate_development: RAGEndToEndSummary | None = None
    # frozen_candidate_matches同时要求指纹一致和开发门通过。
    frozen_candidate_matches: bool = False
    # holdout只在双确认且冻结匹配时出现。
    baseline_holdout: RAGEndToEndSummary | None = None
    candidate_holdout: RAGEndToEndSummary | None = None


class _BatchCachedEmbeddingClient:
    """预先批量向量化问题，禁止逐题隐式调用造成额外费用。"""

    def __init__(self, delegate: EmbeddingClient) -> None:
        """保存真实客户端并初始化空查询缓存。"""

        # _delegate负责文档和首次问题批量向量化。
        self._delegate = delegate
        # _query_vectors使用问题原文作为稳定缓存键。
        self._query_vectors: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        """向Qdrant暴露底层固定向量维度。"""

        # 文档和问题必须处于同一个向量空间。
        return self._delegate.dimension

    def preload_queries(self, questions: Sequence[str]) -> None:
        """一次批量处理新问题，重复评测不再次收费。"""

        # dict.fromkeys去重同时保留数据集首次出现顺序。
        missing = [
            question
            for question in dict.fromkeys(questions)
            if question not in self._query_vectors
        ]
        # 没有新问题时不访问外部服务。
        if not missing:
            # 提前返回保持请求计数不变。
            return
        # 查询和文档使用同一Embedding列表接口与模型参数。
        vectors = self._delegate.embed_documents(missing)
        # strict zip防止数量错位被静默忽略。
        for question, vector in zip(missing, vectors, strict=True):
            # 每个问题缓存一条向量。
            self._query_vectors[question] = vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """知识建库直接委托真实客户端。"""

        # 文档只在本次内存索引创建时向量化一次。
        return self._delegate.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """查询阶段只允许读取已经付费生成的缓存。"""

        # 缺失表示实验器违反费用计划，不能临时发起隐藏请求。
        if text not in self._query_vectors:
            # 错误不包含密钥和向量。
            raise ValueError(f"查询未预加载，禁止隐式Embedding调用：{text}")
        # 返回缓存向量供本地Qdrant搜索。
        return self._query_vectors[text]


def load_rag_end_to_end_cases(path: Path) -> list[RAGEndToEndCase]:
    """读取端到端数据集并校验正负例、ID与问题唯一性。"""

    # TypeAdapter校验顶层JSON数组和每个强类型Case。
    cases = TypeAdapter(list[RAGEndToEndCase]).validate_json(
        path.read_text(encoding="utf-8")
    )
    # 空数据集不能产生有效比例。
    if not cases:
        # 要求数据作者至少添加一条样本。
        raise ValueError("端到端RAG评测集不能为空")
    # 必须同时有正负例才能约束可用性和安全性。
    if not any(case.should_answer for case in cases) or not any(
        not case.should_answer for case in cases
    ):
        # 防止只测试容易的单侧数据。
        raise ValueError("端到端RAG评测集必须同时包含可回答和不可回答样本")
    # ID和问题都不允许重复，避免同一题被重复计权。
    case_ids = [case.case_id for case in cases]
    questions = [case.question for case in cases]
    # 重复ID会破坏Bad Case定位。
    if len(case_ids) != len(set(case_ids)):
        # 要求数据作者修改稳定ID。
        raise ValueError("端到端RAG case_id不能重复")
    # 重复问题会让指标看起来比实际更稳定。
    if len(questions) != len(set(questions)):
        # 要求删除重复题或改为真正的新表达。
        raise ValueError("端到端RAG question不能重复")
    # 返回经过完整校验的数据。
    return cases


def load_rag_end_to_end_experiment_config(path: Path) -> RAGEndToEndExperimentConfig:
    """读取并校验第29步JSON实验契约。"""

    # UTF-8确保中文路径和未来说明字段可读。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic同时检查字段类型和组合关系。
    return RAGEndToEndExperimentConfig.model_validate_json(raw_json)


def rag_end_to_end_candidate_fingerprint(config: RAGEndToEndExperimentConfig) -> str:
    """计算会影响候选行为的完整配置SHA-256。"""

    # fingerprint_payload只包含真正影响链路输出的版本化参数。
    fingerprint_payload = {
        # 数据和语料版本变化必须产生新指纹。
        "experiment_version": config.version,
        "corpus_path": config.corpus_path,
        "chunk_size": config.chunk_size,
        "chunk_overlap": config.chunk_overlap,
        # 检索与重排参数必须冻结。
        "candidate_k": config.candidate_k,
        "top_k": config.top_k,
        "embedding_model": config.candidate_embedding_model,
        "embedding_dimensions": config.candidate_embedding_dimensions,
        "score_threshold": config.candidate_score_threshold,
        "bm25_lexical_weight": config.bm25_lexical_weight,
        # 回答提示是证据判断行为的核心组成。
        "grounding_prompt": GROUNDED_ANSWER_SYSTEM_PROMPT,
        # Profile名称变化也应显式产生新候选身份。
        "candidate_profile_id": config.candidate_profile_id,
    }
    # sort_keys和紧凑分隔符让不同机器得到相同字节序列。
    serialized = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 返回64位小写十六进制指纹。
    return sha256(serialized.encode("utf-8")).hexdigest()


def _quality_gate_failures(
    summary_values: dict[str, float],
    gate: RAGEndToEndQualityGate,
) -> list[str]:
    """把七项指标转换为稳定失败原因码。"""

    # checks按报告阅读顺序保存“指标、比较方向、阈值、原因码”。
    minimum_checks = (
        ("retrieval_recall", gate.min_retrieval_recall, "retrieval_recall_below_threshold"),
        ("top_1_accuracy", gate.min_top_1_accuracy, "top_1_accuracy_below_threshold"),
        ("answerable_recall", gate.min_answerable_recall, "answerable_recall_below_threshold"),
        (
            "abstention_accuracy",
            gate.min_abstention_accuracy,
            "abstention_accuracy_below_threshold",
        ),
        ("decision_accuracy", gate.min_decision_accuracy, "decision_accuracy_below_threshold"),
        ("citation_validity", gate.min_citation_validity, "citation_validity_below_threshold"),
    )
    # failures收集没有通过的有限原因。
    failures = [
        failure_code
        for metric_name, threshold, failure_code in minimum_checks
        if summary_values[metric_name] < threshold
    ]
    # 无依据回答率是上限约束，方向与其他指标相反。
    if summary_values["unsupported_answer_rate"] > gate.max_unsupported_answer_rate:
        # 加入明确安全失败码。
        failures.append("unsupported_answer_rate_above_threshold")
    # 空列表代表全部门槛通过。
    return failures


def _build_reranking_retriever(
    config: RAGEndToEndExperimentConfig,
    *,
    embedding_client: EmbeddingClient,
    score_threshold: float,
    collection_name: str,
) -> tuple[KnowledgeRetriever, int]:
    """使用治理语料构建内存Qdrant，并包裹冻结BM25候选重排。"""

    # 仓库在向量化前过滤内部草稿和退役政策。
    documents = JsonKnowledgeRepository(
        resolve_project_path(config.corpus_path)
    ).list_indexable_documents()
    # 切片器使用本实验版本固定窗口。
    chunker = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    )
    # chunk_count用于准确计算真实文档Embedding批次数。
    chunk_count = len(chunker.split_documents(documents))
    # 原始Qdrant检索器只负责语义或Hash候选召回和阈值过滤。
    raw_retriever = QdrantKnowledgeRetriever(
        client=create_qdrant_client(":memory:"),
        collection_name=collection_name,
        embedding_client=embedding_client,
        score_threshold=score_threshold,
    )
    # 建立一次性隔离索引。
    raw_retriever.ensure_index(documents, chunker=chunker)
    # 用第26步冻结的BM25权重重排原始候选。
    reranking_retriever = RerankingKnowledgeRetriever(
        retriever=raw_retriever,
        reranker=BM25CandidateReranker(lexical_weight=config.bm25_lexical_weight),
        candidate_k=config.candidate_k,
    )
    # 返回完整检索器和切片规模。
    return reranking_retriever, chunk_count


async def evaluate_rag_end_to_end_pipeline(
    *,
    profile_id: str,
    dataset: Literal["development", "holdout"],
    cases: Sequence[RAGEndToEndCase],
    query_policy: KnowledgeQueryPolicy,
    retriever: KnowledgeRetriever,
    answer_client: GroundedAnswerClient,
    top_k: int,
    gate: RAGEndToEndQualityGate,
) -> RAGEndToEndSummary:
    """让每条问题真实经过全部公开决策层，并计算端到端指标。"""

    # results按数据集顺序保存，方便多次报告diff。
    results: list[RAGEndToEndCaseResult] = []
    # 正负例数量是多个指标的分母。
    answerable_cases = sum(1 for case in cases if case.should_answer)
    unanswerable_cases = len(cases) - answerable_cases
    # 以下整数计数器最后统一换算成比例。
    retrieved_answerable = 0
    top_1_answerable = 0
    answered_answerable = 0
    abstained_unanswerable = 0
    correct_decisions = 0
    unsupported_answers = 0
    predicted_answer_count = 0
    valid_citation_answer_count = 0
    grounding_chat_calls = 0

    # 每条题从范围门开始执行，不把人工标签泄漏给流水线。
    for case in cases:
        # assessment是确定性、可解释的范围判断。
        assessment = query_policy.assess(case.question)
        # 初始化后续结果；被前置拒绝时保持空证据。
        hits: list[RetrievalHit] = []
        # 默认草稿表示不回答，只有真实进入回答器才覆盖。
        draft = GroundedAnswerDraft(
            answer="当前证据不足，无法生成可靠回答。",
            citation_ids=[],
            is_answerable=False,
        )
        # terminal_stage记录用户请求停在哪一层。
        terminal_stage: Literal[
            "scope_rejected", "retrieval_empty", "grounding_declined", "answered"
        ]

        # 范围门拒绝时既不调用Embedding，也不调用聊天模型。
        if not assessment.allowed:
            # 公开执行轨迹停在范围门。
            terminal_stage = "scope_rejected"
        else:
            # 允许请求进入真实Qdrant召回和BM25重排。
            hits = retriever.search(case.question, top_k=top_k)
            # 空证据直接安全拒答，不浪费聊天调用。
            if not hits:
                # 公开执行轨迹停在证据阈值。
                terminal_stage = "retrieval_empty"
            else:
                # 只有存在候选证据时才调用回答器。
                draft = await answer_client.generate(question=case.question, evidence=hits)
                # 记录一次真实或基线回答器调用。
                grounding_chat_calls += 1
                # 根据结构化可回答标记选择最终阶段。
                terminal_stage = "answered" if draft.is_answerable else "grounding_declined"

        # retrieved_document_ids保留重排后的父文档顺序，允许同文档多Chunk去重。
        retrieved_document_ids = list(
            dict.fromkeys(hit.chunk.document_id for hit in hits)
        )
        # 正例检索指标只判断人工正确文档是否进入候选。
        expected_documents = set(case.expected_document_ids)
        # retrieval_correct表示至少一份正确文档进入Top-K。
        retrieval_correct = bool(expected_documents.intersection(retrieved_document_ids))
        # top_1_correct要求第一份候选父文档属于人工正确集合。
        top_1_correct = bool(
            retrieved_document_ids and retrieved_document_ids[0] in expected_documents
        )
        # 正例才累计检索召回和首位准确数。
        if case.should_answer:
            # bool可以安全转成0或1累加。
            retrieved_answerable += int(retrieval_correct)
            top_1_answerable += int(top_1_correct)

        # allowed_chunk_ids是最终回答唯一合法引用白名单。
        allowed_chunk_ids = {hit.chunk.chunk_id for hit in hits}
        # citation_ids去重但保持模型返回顺序。
        citation_ids = list(dict.fromkeys(draft.citation_ids))
        # 放行答案必须至少一条引用且全部来自本轮候选。
        citation_valid = bool(citation_ids) and set(citation_ids).issubset(
            allowed_chunk_ids
        )
        # chunk_to_document把合法引用还原到人工标注的父文档。
        chunk_to_document = {hit.chunk.chunk_id: hit.chunk.document_id for hit in hits}
        # cited_document_ids只解析本轮候选中的合法ID；越界ID不会造成KeyError。
        cited_document_ids = list(
            dict.fromkeys(
                chunk_to_document[citation_id]
                for citation_id in citation_ids
                if citation_id in chunk_to_document
            )
        )
        # cited_expected要求正例答案实际引用正确来源，而不只是候选中碰巧存在。
        cited_expected = bool(expected_documents.intersection(cited_document_ids))
        # failures保存当前题最接近根因的有限代码。
        failures: list[str] = []

        # 所有自动回答都进入引用合法率统计。
        if draft.is_answerable:
            # 自动回答分母加一。
            predicted_answer_count += 1
            # 合法引用分子加一。
            if citation_valid:
                valid_citation_answer_count += 1
            else:
                # 引用缺失或越界会被线上确定性节点拦截。
                failures.append("invalid_or_missing_citation")

        # 正例必须最终回答、引用合法且引用正确父文档才算成功。
        if case.should_answer:
            # 依次记录最早失败层，帮助判断该优化哪一段。
            if not assessment.allowed:
                failures.append("scope_false_rejection")
            elif not retrieval_correct:
                failures.append("retrieval_miss")
            elif not draft.is_answerable:
                failures.append("grounding_declined_answerable_case")
            elif not cited_expected:
                failures.append("answer_did_not_cite_expected_document")
            # answerable_pass是业务成功的完整条件。
            answerable_pass = draft.is_answerable and citation_valid and cited_expected
            # 成功时同时计入正例召回和综合决策。
            if answerable_pass:
                answered_answerable += 1
                correct_decisions += 1
            # 逐题通过状态等于完整正例条件。
            passed = answerable_pass
        else:
            # 负例只要最终不自动回答就算安全拒答。
            unanswerable_pass = not draft.is_answerable
            # 正确拒答时累计安全和综合指标。
            if unanswerable_pass:
                abstained_unanswerable += 1
                correct_decisions += 1
            else:
                # 错误放行是端到端无依据回答。
                unsupported_answers += 1
                failures.append("unsupported_answer_generated")
            # 逐题通过状态等于正确拒答。
            passed = unanswerable_pass

        # 保存不含答案正文和隐藏推理的公开工程轨迹。
        results.append(
            RAGEndToEndCaseResult(
                case_id=case.case_id,
                should_answer=case.should_answer,
                scope_allowed=assessment.allowed,
                scope_reason_code=assessment.reason_code,
                retrieved_document_ids=retrieved_document_ids,
                cited_document_ids=cited_document_ids,
                predicted_answerable=draft.is_answerable,
                citation_valid=citation_valid,
                terminal_stage=terminal_stage,
                passed=passed,
                failure_codes=list(dict.fromkeys(failures)),
            )
        )

    # 统一计算七项比例，正负例已由加载器保证非零。
    summary_values = {
        "retrieval_recall": retrieved_answerable / answerable_cases,
        "top_1_accuracy": top_1_answerable / answerable_cases,
        "answerable_recall": answered_answerable / answerable_cases,
        "abstention_accuracy": abstained_unanswerable / unanswerable_cases,
        "decision_accuracy": correct_decisions / len(cases),
        "unsupported_answer_rate": unsupported_answers / unanswerable_cases,
        # 全拒答时引用率约定为1，但answerable_recall会阻止其通过质量门。
        "citation_validity": (
            valid_citation_answer_count / predicted_answer_count
            if predicted_answer_count
            else 1.0
        ),
    }
    # 用预先声明门槛计算晋级失败原因。
    gate_failures = _quality_gate_failures(summary_values, gate)
    # 返回强类型汇总。
    return RAGEndToEndSummary(
        profile_id=profile_id,
        dataset=dataset,
        total_cases=len(cases),
        answerable_cases=answerable_cases,
        unanswerable_cases=unanswerable_cases,
        grounding_chat_calls=grounding_chat_calls,
        quality_gate_passed=not gate_failures,
        quality_gate_failures=gate_failures,
        results=results,
        **summary_values,
    )


def _allowed_questions(
    cases: Sequence[RAGEndToEndCase],
    policy: KnowledgeQueryPolicy,
) -> list[str]:
    """只返回通过范围门的问题，避免把敏感请求发送给Embedding服务商。"""

    # 逐题复用和线上相同的确定性策略。
    return [case.question for case in cases if policy.assess(case.question).allowed]


async def run_rag_end_to_end_experiment(
    config: RAGEndToEndExperimentConfig,
    *,
    runtime_settings: Settings,
    confirm_paid_api: bool = False,
    include_holdout: bool = False,
) -> RAGEndToEndExperimentReport:
    """默认运行离线整链基线；双确认后运行真实候选和锁定集。"""

    # 开发集始终可见并允许调试。
    development_cases = load_rag_end_to_end_cases(
        resolve_project_path(config.development_dataset_path)
    )
    # 两套Profile复用同一确定性范围门。
    query_policy = DeterministicFAQScopePolicy()
    # 当前候选指纹覆盖Embedding、阈值、重排、提示和切片参数。
    candidate_fingerprint = rag_end_to_end_candidate_fingerprint(config)
    # 只统计会真正进入Embedding的开发问题。
    allowed_development_questions = _allowed_questions(development_cases, query_policy)

    # 构建完全离线Hash+BM25基线。
    baseline_retriever, chunk_count = _build_reranking_retriever(
        config,
        embedding_client=HashEmbeddingClient(config.baseline_embedding_dimensions),
        score_threshold=config.baseline_score_threshold,
        collection_name="serviceops-e2e-baseline",
    )
    # 旧Extractive只要检索非空就回答，作为当前风险对照。
    baseline_development = await evaluate_rag_end_to_end_pipeline(
        profile_id="hash-bm25-extractive-baseline",
        dataset="development",
        cases=development_cases,
        query_policy=query_policy,
        retriever=baseline_retriever,
        answer_client=ExtractiveGroundedAnswerClient(),
        top_k=config.top_k,
        gate=config.development_gate,
    )

    # 文档批次加开发问题批次就是第一次候选Embedding计划。
    planned_development_embedding_requests = ceil(
        chunk_count / config.candidate_embedding_batch_size
    ) + ceil(len(allowed_development_questions) / config.candidate_embedding_batch_size)
    # 聊天调用上界等于通过范围门的问题数；空检索会进一步减少实际调用。
    planned_development_chat_calls = len(allowed_development_questions)
    # holdout最坏只增加一批查询Embedding和每题一次聊天。
    planned_holdout_extra_embedding_requests = ceil(
        config.holdout_case_count / config.candidate_embedding_batch_size
    )
    planned_holdout_extra_chat_calls = config.holdout_case_count

    # 默认路径不读取密钥、不构建真实索引，也不读取holdout。
    if not confirm_paid_api:
        # 返回可在CI稳定运行的离线报告。
        return RAGEndToEndExperimentReport(
            experiment_id=config.experiment_id,
            experiment_version=config.version,
            candidate_profile_id=config.candidate_profile_id,
            candidate_fingerprint=candidate_fingerprint,
            embedding_model=config.candidate_embedding_model,
            chat_model=runtime_settings.llm_model,
            planned_development_embedding_requests=planned_development_embedding_requests,
            planned_development_chat_calls=planned_development_chat_calls,
            planned_holdout_extra_embedding_requests=planned_holdout_extra_embedding_requests,
            planned_holdout_extra_chat_calls=planned_holdout_extra_chat_calls,
            paid_api_called=False,
            actual_embedding_requests=0,
            actual_embedding_input_tokens=0,
            actual_chat_calls=0,
            baseline_development=baseline_development,
        )

    # 真实候选必须有密钥和兼容地址。
    api_key = (
        runtime_settings.llm_api_key.get_secret_value()
        if runtime_settings.llm_api_key
        else ""
    )
    # 缺Key时在任何外部调用前给出明确提示。
    if not api_key:
        # 不泄漏任何环境变量值。
        raise ValueError("第29步真实候选需要SERVICEOPS_LLM_API_KEY")
    # Base URL必须与密钥地域匹配。
    if not runtime_settings.llm_base_url:
        # 快速失败避免难理解的SDK地址错误。
        raise ValueError("第29步真实候选需要SERVICEOPS_LLM_BASE_URL")

    # 创建可计数的真实千问Embedding适配器。
    raw_embedding_client = OpenAICompatibleEmbeddingClient(
        api_key=api_key,
        base_url=runtime_settings.llm_base_url,
        model=config.candidate_embedding_model,
        dimension=config.candidate_embedding_dimensions,
        batch_size=config.candidate_embedding_batch_size,
        timeout_seconds=runtime_settings.llm_timeout_seconds,
        max_retries=runtime_settings.llm_max_retries,
    )
    # 缓存包装器保证开发和阈值搜索不会逐题重复收费。
    cached_embedding_client = _BatchCachedEmbeddingClient(raw_embedding_client)
    # 建库会先产生文档Embedding请求。
    candidate_retriever, _ = _build_reranking_retriever(
        config,
        embedding_client=cached_embedding_client,
        score_threshold=config.candidate_score_threshold,
        collection_name="serviceops-e2e-candidate",
    )
    # 再按批次预加载所有通过范围门的开发问题。
    cached_embedding_client.preload_queries(allowed_development_questions)
    # 强制聊天后端使用现有OpenAI兼容千问与Grounded结构化输出。
    candidate_settings = runtime_settings.model_copy(
        update={"llm_backend": "openai_compatible", "rag_generation_backend": "llm"}
    )
    # 工厂复用线上聊天模型、超时、重试和上下文预算。
    candidate_answer_client = create_grounded_answer_client(candidate_settings)
    # 运行真实组合候选开发集。
    candidate_development = await evaluate_rag_end_to_end_pipeline(
        profile_id=config.candidate_profile_id,
        dataset="development",
        cases=development_cases,
        query_policy=query_policy,
        retriever=candidate_retriever,
        answer_client=candidate_answer_client,
        top_k=config.top_k,
        gate=config.development_gate,
    )
    # 只有开发门通过且配置中的冻结指纹完全一致才允许读取holdout。
    frozen_candidate_matches = (
        config.frozen_candidate_fingerprint == candidate_fingerprint
        and candidate_development.quality_gate_passed
    )
    # 默认不生成锁定结果。
    baseline_holdout: RAGEndToEndSummary | None = None
    candidate_holdout: RAGEndToEndSummary | None = None

    # 第二把钥匙出现时才进入锁定路径。
    if include_holdout:
        # 指纹未冻结或开发门失败时禁止读取锁定集。
        if not frozen_candidate_matches:
            # 提醒用户先审查开发Bad Case，而不是偷看holdout调参。
            raise ValueError("端到端候选尚未冻结或开发质量门未通过，禁止运行holdout")
        # 此时才读取全新锁定数据。
        holdout_cases = load_rag_end_to_end_cases(
            resolve_project_path(config.holdout_dataset_path)
        )
        # 文件数量必须与付费前计划一致。
        if len(holdout_cases) != config.holdout_case_count:
            # 防止确认后临时扩大费用和分母。
            raise ValueError("端到端holdout实际数量与配置计划不一致")
        # 基线锁定对照使用独立Hash索引，避免依赖候选缓存。
        baseline_holdout_retriever, _ = _build_reranking_retriever(
            config,
            embedding_client=HashEmbeddingClient(config.baseline_embedding_dimensions),
            score_threshold=config.baseline_score_threshold,
            collection_name="serviceops-e2e-baseline-holdout",
        )
        # 运行同一锁定集的离线当前链路。
        baseline_holdout = await evaluate_rag_end_to_end_pipeline(
            profile_id="hash-bm25-extractive-baseline",
            dataset="holdout",
            cases=holdout_cases,
            query_policy=query_policy,
            retriever=baseline_holdout_retriever,
            answer_client=ExtractiveGroundedAnswerClient(),
            top_k=config.top_k,
            gate=config.holdout_gate,
        )
        # 只向量化通过范围门的新锁定问题。
        allowed_holdout_questions = _allowed_questions(holdout_cases, query_policy)
        # 加入已有查询缓存，不重建文档向量。
        cached_embedding_client.preload_queries(allowed_holdout_questions)
        # 使用冻结候选执行唯一一次端到端锁定验收。
        candidate_holdout = await evaluate_rag_end_to_end_pipeline(
            profile_id=config.candidate_profile_id,
            dataset="holdout",
            cases=holdout_cases,
            query_policy=query_policy,
            retriever=candidate_retriever,
            answer_client=candidate_answer_client,
            top_k=config.top_k,
            gate=config.holdout_gate,
        )

    # 实际聊天数来自开发和可选锁定评测器计数。
    actual_chat_calls = candidate_development.grounding_chat_calls + (
        candidate_holdout.grounding_chat_calls if candidate_holdout else 0
    )
    # 返回完整、可序列化报告。
    return RAGEndToEndExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        candidate_profile_id=config.candidate_profile_id,
        candidate_fingerprint=candidate_fingerprint,
        embedding_model=config.candidate_embedding_model,
        chat_model=candidate_settings.llm_model,
        planned_development_embedding_requests=planned_development_embedding_requests,
        planned_development_chat_calls=planned_development_chat_calls,
        planned_holdout_extra_embedding_requests=planned_holdout_extra_embedding_requests,
        planned_holdout_extra_chat_calls=planned_holdout_extra_chat_calls,
        paid_api_called=True,
        actual_embedding_requests=raw_embedding_client.api_request_count,
        actual_embedding_input_tokens=raw_embedding_client.input_token_count,
        actual_chat_calls=actual_chat_calls,
        baseline_development=baseline_development,
        candidate_development=candidate_development,
        frozen_candidate_matches=frozen_candidate_matches,
        baseline_holdout=baseline_holdout,
        candidate_holdout=candidate_holdout,
    )
