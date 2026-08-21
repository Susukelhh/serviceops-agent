"""第27步：Hash词面向量与千问真实语义向量的受控候选实验。"""

# Sequence用于统一接收评测样本。
from collections.abc import Sequence

# ceil计算计划批次数；datetime记录报告生成时间。
from datetime import UTC, datetime
from math import ceil
from pathlib import Path

# Literal限制数据集和后端名称。
from typing import Literal

# Pydantic校验实验配置、质量门和报告结构。
from pydantic import BaseModel, Field, model_validator

# 项目路径解析保证PyCharm工作目录变化不影响数据定位。
from serviceops_agent.config.paths import resolve_project_path

# 项目配置提供真实千问密钥、地址、超时和重试参数。
from serviceops_agent.config.settings import Settings

# RetrievalHit是阈值包装器返回的稳定领域类型。
from serviceops_agent.domain.knowledge import RetrievalHit

# 通用评测器负责Recall、Top-1、MRR、nDCG、决策准确率和误召回率。
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)

# JSON知识仓库在索引前过滤草稿、退役和内部文档。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# 切片器保持第24步冻结的500字符窗口与80字符重叠。
from serviceops_agent.rag.chunking import KnowledgeChunker

# 两种Embedding客户端共享同一个最小协议。
from serviceops_agent.rag.embeddings import (
    EmbeddingClient,
    HashEmbeddingClient,
    OpenAICompatibleEmbeddingClient,
)

# Qdrant检索器和本地客户端用于同语料、同切片、同Top-K比较。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    QdrantKnowledgeRetriever,
    create_qdrant_client,
)


class RAGSemanticQualityGate(BaseModel):
    """真实语义候选必须同时达到的召回、排序和拒答边界。"""

    # min_recall_at_k防止同义改写问题找不到正确知识。
    min_recall_at_k: float = Field(ge=0.0, le=1.0)
    # min_top_1_accuracy要求首条证据大多数时候就是正确文档。
    min_top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # min_decision_accuracy同时考察正例回答和知识缺口拒答。
    min_decision_accuracy: float = Field(ge=0.0, le=1.0)
    # max_false_positive_rate限制没有知识依据时仍返回文档的比例。
    max_false_positive_rate: float = Field(ge=0.0, le=1.0)


class RAGSemanticEmbeddingExperimentConfig(BaseModel):
    """版本化的语义Embedding开发、冻结和锁定验收契约。"""

    # experiment_id稳定标识本实验系列。
    experiment_id: str = Field(min_length=1, max_length=100)
    # version在数据、模型、阈值集合或判定门变化时递增。
    version: str = Field(min_length=1, max_length=50)
    # corpus_path指向第24步困难知识语料。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_path可以用于选择阈值。
    development_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_dataset_path只允许冻结后读取。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_case_count只用于付费前估算请求数，不提前读取锁定集内容。
    holdout_case_count: int = Field(ge=1, le=1000)
    # embedding_model记录接受评测的真实模型ID。
    embedding_model: str = Field(min_length=1, max_length=100)
    # embedding_dimensions固定向量空间大小。
    embedding_dimensions: int = Field(ge=64, le=2560)
    # embedding_batch_size必须符合当前服务商模型限制。
    embedding_batch_size: int = Field(ge=1, le=100)
    # input_price用于把服务商返回Token换算成可审计成本。
    input_price_cny_per_million_tokens: float = Field(ge=0.0, le=100.0)
    # chunk_size保持本轮单变量实验的切片大小不变。
    chunk_size: int = Field(ge=100, le=2000)
    # chunk_overlap保持切片重叠不变。
    chunk_overlap: int = Field(ge=0, le=500)
    # candidate_k是Qdrant原始候选池大小。
    candidate_k: int = Field(ge=1, le=20)
    # top_k是统一指标的最终观察范围。
    top_k: int = Field(ge=1, le=20)
    # threshold_candidates只在本地过滤已缓存结果，不会重复调用Embedding。
    threshold_candidates: list[float] = Field(min_length=2, max_length=20)
    # frozen_candidate_threshold第一次开发实验前保持None，防止偷看锁定集。
    frozen_candidate_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    # development_gate用于开发候选晋级。
    development_gate: RAGSemanticQualityGate
    # holdout_gate用于冻结候选最终验收。
    holdout_gate: RAGSemanticQualityGate

    @model_validator(mode="after")
    def validate_experiment_relationships(self) -> "RAGSemanticEmbeddingExperimentConfig":
        """校验切片、候选池、阈值和已知千问批次限制。"""

        # overlap覆盖整个窗口会让切片起点无法正常前进。
        if self.chunk_overlap >= self.chunk_size:
            # 在读配置时直接给出可理解错误。
            raise ValueError("chunk_overlap必须小于chunk_size")
        # 原候选池不能小于最终Top-K。
        if self.candidate_k < self.top_k:
            # 否则不同Profile实际看到的候选范围会不一致。
            raise ValueError("candidate_k不能小于top_k")
        # 阈值必须严格递增，避免报告出现重复或顺序不稳定。
        if self.threshold_candidates != sorted(set(self.threshold_candidates)):
            # 要求调用者整理后再运行。
            raise ValueError("threshold_candidates必须去重并按升序排列")
        # 当前官方qwen3.7同步接口单次最多20条，配置更大会产生400错误。
        if self.embedding_model == "qwen3.7-text-embedding" and self.embedding_batch_size > 20:
            # 该规则随实验版本和官方限制一起维护。
            raise ValueError("qwen3.7-text-embedding单批最多20条")
        # 返回通过组合校验的配置。
        return self


class RAGSemanticProfileResult(BaseModel):
    """某个Embedding后端与阈值在一份数据集上的结果。"""

    # profile_id组合后端和阈值，便于报告比较。
    profile_id: str
    # dataset区分可调开发集和一次性锁定集。
    dataset: Literal["development", "holdout"]
    # embedding_backend说明是本地Hash还是千问真实语义向量。
    embedding_backend: Literal["hash", "qwen"]
    # score_threshold是当前本地证据门阈值。
    score_threshold: float = Field(ge=0.0, le=1.0)
    # metrics保存统一检索指标和逐样本结果。
    metrics: RAGEvaluationSummary
    # quality_gate_passed表示是否满足当前数据集全部门槛。
    quality_gate_passed: bool
    # quality_gate_failures保存稳定、可聚合的失败原因码。
    quality_gate_failures: list[str]


class RAGSemanticEmbeddingExperimentReport(BaseModel):
    """离线基线、可选真实候选、费用和锁定结果的完整报告。"""

    # experiment_id和version定位实验契约。
    experiment_id: str
    experiment_version: str
    # generated_at使用UTC，避免本地时区造成比较混乱。
    generated_at: datetime
    # model记录真实候选名称；未调用时仍可展示计划。
    model: str
    # document_count是治理过滤后进入公共索引的文档数。
    document_count: int = Field(ge=1)
    # chunk_count是实际Embedding的知识切片数。
    chunk_count: int = Field(ge=1)
    # planned_development_api_requests按批次准确计算业务层计划请求数。
    planned_development_api_requests: int = Field(ge=1)
    # planned_holdout_extra_api_requests说明冻结后增量查询请求数。
    planned_holdout_extra_api_requests: int = Field(ge=1)
    # paid_api_called只有真正构建千问索引时才为True。
    paid_api_called: bool
    # actual_api_requests来自适配器成功响应计数，不含SDK内部重试。
    actual_api_requests: int = Field(ge=0)
    # actual_input_tokens来自服务商usage。
    actual_input_tokens: int = Field(ge=0)
    # actual_cost_cny按版本化单价换算。
    actual_cost_cny: float = Field(ge=0.0)
    # hash_development_results始终存在，保证无Key也可运行。
    hash_development_results: list[RAGSemanticProfileResult]
    # hash_selected_threshold是Hash在开发集上的最佳公平对照阈值。
    hash_selected_threshold: float
    # qwen_development_results只在显式确认付费后出现。
    qwen_development_results: list[RAGSemanticProfileResult] = Field(default_factory=list)
    # qwen_selected_threshold只由开发集选出。
    qwen_selected_threshold: float | None = None
    # frozen_threshold_matches_selection控制锁定集权限。
    frozen_threshold_matches_selection: bool = False
    # hash_holdout和qwen_holdout只在冻结后显式确认时出现。
    hash_holdout: RAGSemanticProfileResult | None = None
    qwen_holdout: RAGSemanticProfileResult | None = None


class _BatchCachedEmbeddingClient:
    """提前批量向量化问题，后续阈值扫描只读取内存缓存。"""

    def __init__(self, delegate: EmbeddingClient) -> None:
        """保存真实或Hash实现，并初始化空查询缓存。"""

        # _delegate负责文档向量化和首次问题批量向量化。
        self._delegate = delegate
        # _query_vectors使用问题原文作为稳定键。
        self._query_vectors: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        """向Qdrant暴露底层固定维度。"""

        # 文档和问题仍处于同一个向量空间。
        return self._delegate.dimension

    def preload_queries(self, questions: Sequence[str]) -> None:
        """把未缓存问题一次按批次向量化，避免每个阈值重复收费。"""

        # 保留输入顺序，同时跳过已经缓存的问题。
        missing_questions = [
            question for question in questions if question not in self._query_vectors
        ]
        # 没有新问题时不调用任何Embedding。
        if not missing_questions:
            # 提前结束保持费用为零。
            return
        # embed_documents本质调用同一OpenAI Embeddings列表接口，可利用批次上限。
        vectors = self._delegate.embed_documents(missing_questions)
        # strict zip防止数量不一致被静默截断。
        for question, vector in zip(missing_questions, vectors, strict=True):
            # 每个问题只保存一条向量。
            self._query_vectors[question] = vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """知识建库仍委托底层客户端按批次处理。"""

        # 不缓存文档是因为同一内存索引只创建一次。
        return self._delegate.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """只返回预加载向量，禁止阈值扫描产生隐藏API调用。"""

        # 缺失说明实验器忘记预加载，应失败而不是临时收费。
        if text not in self._query_vectors:
            # 错误不包含密钥，只指出问题原文未进入版本化流程。
            raise ValueError(f"查询未预加载，禁止隐式Embedding调用：{text}")
        # 返回缓存向量；Qdrant只读，不会修改列表。
        return self._query_vectors[text]


class _ScoreThresholdRetriever:
    """在同一原始Top-K结果上本地扫描证据阈值。"""

    def __init__(
        self,
        retriever: KnowledgeRetriever,
        *,
        threshold: float,
        candidate_k: int,
    ) -> None:
        """保存原始检索器、当前阈值与固定候选池。"""

        # _retriever本身使用0阈值返回原始候选。
        self._retriever = retriever
        # _threshold是本Profile唯一变量。
        self._threshold = threshold
        # _candidate_k确保所有阈值看到同样数量上限。
        self._candidate_k = candidate_k

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """取得固定候选后按分数过滤，不改变原排序。"""

        # Qdrant本地查询使用缓存向量，不发生外部调用。
        raw_hits = self._retriever.search(query, top_k=max(top_k, self._candidate_k))
        # 只保留达到当前证据阈值的命中，并限制最终Top-K。
        return [hit for hit in raw_hits if hit.score >= self._threshold][:top_k]


def load_rag_semantic_embedding_experiment_config(
    path: Path,
) -> RAGSemanticEmbeddingExperimentConfig:
    """读取并校验第27步JSON实验契约。"""

    # 明确UTF-8以支持中文路径和未来描述字段。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic同时校验字段和组合关系。
    return RAGSemanticEmbeddingExperimentConfig.model_validate_json(raw_json)


def _gate_failures(
    metrics: RAGEvaluationSummary,
    gate: RAGSemanticQualityGate,
) -> list[str]:
    """把四项质量门转换为稳定失败原因。"""

    # failures按固定顺序生成，便于报告diff。
    failures: list[str] = []
    # 召回不足表示语义问题仍找不到正确文档。
    if metrics.recall_at_k < gate.min_recall_at_k:
        # 写入稳定原因码。
        failures.append("recall_at_k_below_threshold")
    # 首位准确率不足表示上下文第一条经常错误。
    if metrics.top_1_accuracy < gate.min_top_1_accuracy:
        # 写入稳定原因码。
        failures.append("top_1_accuracy_below_threshold")
    # 综合决策不足表示回答和拒答总体不可靠。
    if metrics.decision_accuracy < gate.min_decision_accuracy:
        # 写入稳定原因码。
        failures.append("decision_accuracy_below_threshold")
    # 误召回超过上限意味着知识缺口时仍可能编造依据。
    if metrics.false_positive_rate > gate.max_false_positive_rate:
        # 写入稳定原因码。
        failures.append("false_positive_rate_above_threshold")
    # 空列表表示全部通过。
    return failures


def _evaluate_thresholds(
    raw_retriever: KnowledgeRetriever,
    cases: Sequence[RAGEvaluationCase],
    *,
    dataset: Literal["development", "holdout"],
    backend: Literal["hash", "qwen"],
    thresholds: Sequence[float],
    candidate_k: int,
    top_k: int,
    gate: RAGSemanticQualityGate,
) -> list[RAGSemanticProfileResult]:
    """复用同一缓存向量与Qdrant索引扫描全部阈值。"""

    # results按配置阈值升序保存。
    results: list[RAGSemanticProfileResult] = []
    # 每个阈值只增加本地Qdrant查询和列表过滤。
    for threshold in thresholds:
        # 包装原始0阈值检索器。
        threshold_retriever = _ScoreThresholdRetriever(
            # 复用相同索引。
            raw_retriever,
            # 当前唯一变量。
            threshold=threshold,
            # 固定候选池。
            candidate_k=candidate_k,
        )
        # 执行统一指标计算。
        metrics = evaluate_retriever(threshold_retriever, cases, top_k=top_k)
        # 计算当前数据集质量门。
        failures = _gate_failures(metrics, gate)
        # 保存强类型结果。
        results.append(
            RAGSemanticProfileResult(
                # 阈值保留两位小数形成稳定名称。
                profile_id=f"{backend}-threshold-{threshold:.2f}",
                # 保存数据集类型。
                dataset=dataset,
                # 保存Embedding来源。
                embedding_backend=backend,
                # 保存实际阈值。
                score_threshold=threshold,
                # 保存指标和逐样本明细。
                metrics=metrics,
                # 没有失败原因即通过。
                quality_gate_passed=not failures,
                # 保存原因列表。
                quality_gate_failures=failures,
            )
        )
    # 返回完整阈值曲线，而不是只报最好数字。
    return results


def _select_development_result(
    results: Sequence[RAGSemanticProfileResult],
) -> RAGSemanticProfileResult:
    """只用开发集选择最均衡阈值；没有通过者时返回诊断最佳项。"""

    # eligible优先限制在通过预先声明质量门的候选。
    eligible = [result for result in results if result.quality_gate_passed]
    # 无候选通过时仍返回最佳诊断点，但调用方可从quality_gate_passed识别未晋级。
    selection_pool = eligible or list(results)
    # 先最大化综合决策，再看召回、首位、MRR；同分时选择更高阈值减少误召回。
    return max(
        selection_pool,
        key=lambda result: (
            result.metrics.decision_accuracy,
            result.metrics.recall_at_k,
            result.metrics.top_1_accuracy,
            result.metrics.mrr_at_k,
            -result.metrics.false_positive_rate,
            result.score_threshold,
        ),
    )


def _build_raw_retriever(
    config: RAGSemanticEmbeddingExperimentConfig,
    *,
    embedding_client: _BatchCachedEmbeddingClient,
    collection_name: str,
) -> tuple[KnowledgeRetriever, int, int]:
    """使用治理语料、冻结切片和0阈值创建一份内存Qdrant索引。"""

    # 仓库读取并过滤允许公开索引的文档。
    documents = JsonKnowledgeRepository(
        resolve_project_path(config.corpus_path)
    ).list_indexable_documents()
    # 创建与前序实验一致的切片器。
    chunker = KnowledgeChunker(
        # 固定500字符窗口。
        chunk_size=config.chunk_size,
        # 固定80字符重叠。
        chunk_overlap=config.chunk_overlap,
    )
    # 预先计算切片数量，用于费用计划和报告；ensure_index会做同一确定性切分。
    chunks = chunker.split_documents(documents)
    # 每个实验后端使用独立内存Qdrant，进程结束自动释放。
    retriever = QdrantKnowledgeRetriever(
        # 创建完全隔离的内存客户端。
        client=create_qdrant_client(":memory:"),
        # Collection名称只需在本客户端内稳定。
        collection_name=collection_name,
        # 使用带查询缓存的Embedding。
        embedding_client=embedding_client,
        # 原始索引设为0阈值，后续全部阈值在本地统一过滤。
        score_threshold=0.0,
    )
    # 只构建一次索引，真实文档向量只收费一次。
    retriever.ensure_index(documents, chunker=chunker)
    # 返回检索器及治理后的规模证据。
    return retriever, len(documents), len(chunks)


def run_rag_semantic_embedding_experiment(
    config: RAGSemanticEmbeddingExperimentConfig,
    *,
    runtime_settings: Settings,
    confirm_paid_api: bool = False,
    include_holdout: bool = False,
) -> RAGSemanticEmbeddingExperimentReport:
    """先运行Hash基线；显式确认后运行千问，并在冻结匹配后允许holdout。"""

    # 开发集始终读取并允许调参。
    development_cases = load_rag_evaluation_cases(
        resolve_project_path(config.development_dataset_path)
    )
    # 先加载治理语料计算准确计划规模，不调用任何模型。
    governed_documents = JsonKnowledgeRepository(
        resolve_project_path(config.corpus_path)
    ).list_indexable_documents()
    # 使用冻结切片参数计算文档批次数。
    chunks = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    ).split_documents(governed_documents)
    # 文档批次加开发问题批次就是第一次真实实验计划请求数。
    planned_development_requests = ceil(len(chunks) / config.embedding_batch_size) + ceil(
        len(development_cases) / config.embedding_batch_size
    )
    # holdout只需为新问题增加批次，复用已经建立的同一索引。
    planned_holdout_extra_requests = ceil(config.holdout_case_count / config.embedding_batch_size)

    # Hash客户端完全离线，但仍走与真实候选相同缓存和Qdrant路径。
    hash_cached = _BatchCachedEmbeddingClient(HashEmbeddingClient(config.embedding_dimensions))
    # 提前批量生成全部开发问题Hash向量。
    hash_cached.preload_queries([case.question for case in development_cases])
    # 建立Hash原始索引。
    hash_retriever, document_count, chunk_count = _build_raw_retriever(
        config,
        embedding_client=hash_cached,
        collection_name="rag_semantic_hash_development",
    )
    # 扫描Hash阈值曲线。
    hash_results = _evaluate_thresholds(
        hash_retriever,
        development_cases,
        dataset="development",
        backend="hash",
        thresholds=config.threshold_candidates,
        candidate_k=config.candidate_k,
        top_k=config.top_k,
        gate=config.development_gate,
    )
    # 选择Hash公平对照点。
    hash_selected = _select_development_result(hash_results)

    # 未显式确认时到此结束，API Key即使存在也不会被读取或调用。
    if not confirm_paid_api:
        # 返回费用为零的计划报告。
        return RAGSemanticEmbeddingExperimentReport(
            experiment_id=config.experiment_id,
            experiment_version=config.version,
            generated_at=datetime.now(UTC),
            model=config.embedding_model,
            document_count=document_count,
            chunk_count=chunk_count,
            planned_development_api_requests=planned_development_requests,
            planned_holdout_extra_api_requests=planned_holdout_extra_requests,
            paid_api_called=False,
            actual_api_requests=0,
            actual_input_tokens=0,
            actual_cost_cny=0.0,
            hash_development_results=hash_results,
            hash_selected_threshold=hash_selected.score_threshold,
        )

    # 真实候选显式覆盖实验模型、维度和批次，不依赖用户日常默认值。
    candidate_settings = runtime_settings.model_copy(
        update={
            "embedding_backend": "openai_compatible",
            "embedding_model": config.embedding_model,
            "embedding_dimensions": config.embedding_dimensions,
            "embedding_batch_size": config.embedding_batch_size,
        }
    )
    # 密钥和Base URL在客户端构建前进行脱敏校验。
    api_key = (
        candidate_settings.llm_api_key.get_secret_value() if candidate_settings.llm_api_key else ""
    )
    # 缺密钥时给出配置字段而不是401长堆栈。
    if not api_key:
        # 不回显实际环境变量内容。
        raise ValueError("真实Embedding实验需要SERVICEOPS_LLM_API_KEY")
    # 千问兼容地址必须存在且与Key地域一致。
    if not candidate_settings.llm_base_url:
        # 明确指出缺失字段。
        raise ValueError("真实Embedding实验需要SERVICEOPS_LLM_BASE_URL")
    # 直接创建可读取实际请求和Token计数的真实适配器。
    qwen_delegate = OpenAICompatibleEmbeddingClient(
        api_key=api_key,
        base_url=candidate_settings.llm_base_url,
        model=config.embedding_model,
        dimension=config.embedding_dimensions,
        batch_size=config.embedding_batch_size,
        timeout_seconds=candidate_settings.llm_timeout_seconds,
        max_retries=candidate_settings.llm_max_retries,
    )
    # 缓存层确保全部阈值只消费一轮开发问题向量。
    qwen_cached = _BatchCachedEmbeddingClient(qwen_delegate)
    # 开发问题最多20条一批发送；本数据集16条，因此只需一个问题请求。
    qwen_cached.preload_queries([case.question for case in development_cases])
    # 文档切片按20条分批建立真实语义索引。
    qwen_retriever, _, _ = _build_raw_retriever(
        config,
        embedding_client=qwen_cached,
        collection_name="rag_semantic_qwen_development",
    )
    # 在已缓存结果上扫描全部阈值。
    qwen_results = _evaluate_thresholds(
        qwen_retriever,
        development_cases,
        dataset="development",
        backend="qwen",
        thresholds=config.threshold_candidates,
        candidate_k=config.candidate_k,
        top_k=config.top_k,
        gate=config.development_gate,
    )
    # 只根据开发集选择真实候选阈值。
    qwen_selected = _select_development_result(qwen_results)
    # 冻结值必须存在、匹配且开发候选通过质量门，才具备holdout资格。
    frozen_match = (
        config.frozen_candidate_threshold is not None
        and abs(config.frozen_candidate_threshold - qwen_selected.score_threshold) < 1e-12
        and qwen_selected.quality_gate_passed
    )
    # 默认不运行任何锁定集结果。
    hash_holdout: RAGSemanticProfileResult | None = None
    qwen_holdout: RAGSemanticProfileResult | None = None

    # 只有显式请求holdout时才进入锁定路径。
    if include_holdout:
        # 开发优胜阈值未冻结时禁止读取和评测锁定集。
        if not frozen_match or config.frozen_candidate_threshold is None:
            # 提醒先根据开发报告更新版本化配置。
            raise ValueError("千问开发优胜阈值尚未冻结或未通过质量门，禁止运行holdout")
        # 此处才正式加载锁定样本。
        holdout_cases = load_rag_evaluation_cases(resolve_project_path(config.holdout_dataset_path))
        # Hash补充锁定问题缓存，不重建索引。
        hash_cached.preload_queries([case.question for case in holdout_cases])
        # 千问把全部锁定问题按批次向量化，仍只执行一次阈值。
        qwen_cached.preload_queries([case.question for case in holdout_cases])
        # 只评测开发阶段已经冻结的一个阈值。
        frozen_thresholds = [config.frozen_candidate_threshold]
        # Hash锁定结果用于同数据集公平对照。
        hash_holdout = _evaluate_thresholds(
            hash_retriever,
            holdout_cases,
            dataset="holdout",
            backend="hash",
            thresholds=frozen_thresholds,
            candidate_k=config.candidate_k,
            top_k=config.top_k,
            gate=config.holdout_gate,
        )[0]
        # 千问锁定结果决定最终是否晋级。
        qwen_holdout = _evaluate_thresholds(
            qwen_retriever,
            holdout_cases,
            dataset="holdout",
            backend="qwen",
            thresholds=frozen_thresholds,
            candidate_k=config.candidate_k,
            top_k=config.top_k,
            gate=config.holdout_gate,
        )[0]

    # 使用服务商返回Token按官方单价计算本次实际费用。
    actual_cost = (
        qwen_delegate.input_token_count * config.input_price_cny_per_million_tokens / 1_000_000
    )
    # 返回真实候选完整报告。
    return RAGSemanticEmbeddingExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        generated_at=datetime.now(UTC),
        model=config.embedding_model,
        document_count=document_count,
        chunk_count=chunk_count,
        planned_development_api_requests=planned_development_requests,
        planned_holdout_extra_api_requests=planned_holdout_extra_requests,
        paid_api_called=True,
        actual_api_requests=qwen_delegate.api_request_count,
        actual_input_tokens=qwen_delegate.input_token_count,
        actual_cost_cny=actual_cost,
        hash_development_results=hash_results,
        hash_selected_threshold=hash_selected.score_threshold,
        qwen_development_results=qwen_results,
        qwen_selected_threshold=qwen_selected.score_threshold,
        frozen_threshold_matches_selection=frozen_match,
        hash_holdout=hash_holdout,
        qwen_holdout=qwen_holdout,
    )
