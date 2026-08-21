"""第26步向量原序与BM25候选重排的开发/锁定实验。"""

# datetime记录UTC报告时间；perf_counter测量查询阶段近似耗时。
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

# Literal限制数据集和重排模式的报告取值。
from typing import Literal

# BaseModel/Field校验实验配置与结果。
from pydantic import BaseModel, Field

# resolve_project_path统一PyCharm与终端路径解析。
from serviceops_agent.config.paths import resolve_project_path

# Settings为每个Profile创建独立离线Qdrant。
from serviceops_agent.config.settings import Settings

# 通用检索评测提供Recall、Top-1、MRR和逐样本排名。
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)

# 范围门装饰器保持第25步已晋级的查询边界，虽然本排序集全部是正例。
from serviceops_agent.rag.query_policy import (
    PolicyFilteredKnowledgeRetriever,
    create_knowledge_query_policy,
)

# BM25重排器只重新排列原候选集合。
from serviceops_agent.rag.reranking import (
    BM25CandidateReranker,
    RerankingKnowledgeRetriever,
)

# KnowledgeRetriever统一原向量候选与重排候选类型。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    build_default_knowledge_retriever,
)


class RAGRerankQualityGate(BaseModel):
    """排序候选必须同时满足的绝对质量和相对提升边界。"""

    # min_recall_at_k防止重排实现意外丢失候选。
    min_recall_at_k: float = Field(ge=0.0, le=1.0)
    # min_top_1_accuracy是最终首条证据的最低准确率。
    min_top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # min_mrr_at_k限制相关文档整体不能排得太后。
    min_mrr_at_k: float = Field(ge=0.0, le=1.0)
    # min_top_1_gain要求候选相对同数据集Baseline有真实绝对提升。
    min_top_1_gain: float = Field(ge=0.0, le=1.0)


class RAGRerankExperimentConfig(BaseModel):
    """版本化的排序开发集、权重候选与锁定验收契约。"""

    # experiment_id标识排序实验系列。
    experiment_id: str = Field(min_length=1, max_length=100)
    # version在数据、公式、权重或质量门变化时递增。
    version: str = Field(min_length=1, max_length=50)
    # corpus_path复用第24步困难知识语料。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_path允许反复进行权重选型。
    development_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_dataset_path只在Profile冻结后运行一次。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # embedding_dimensions保持Hash基线一致。
    embedding_dimensions: int = Field(ge=64, le=2048)
    # chunk_size本步固定不变。
    chunk_size: int = Field(ge=100, le=2000)
    # chunk_overlap本步固定不变。
    chunk_overlap: int = Field(ge=0, le=500)
    # score_threshold保持第25步晋级值0.10。
    score_threshold: float = Field(ge=0.0, le=1.0)
    # candidate_k是允许重排的原候选池大小。
    candidate_k: int = Field(ge=1, le=20)
    # top_k是统一评测的最终返回数。
    top_k: int = Field(ge=1, le=20)
    # lexical_weight_candidates只改变词面融合权重。
    lexical_weight_candidates: list[float] = Field(min_length=2, max_length=10)
    # frozen_candidate_profile_id在holdout前由开发优胜者写入。
    frozen_candidate_profile_id: str = Field(min_length=1, max_length=100)
    # development_gate用于开发选型。
    development_gate: RAGRerankQualityGate
    # holdout_gate用于冻结后的泛化验收。
    holdout_gate: RAGRerankQualityGate


class RAGRerankProfileResult(BaseModel):
    """一个原始或BM25融合Profile在指定数据集上的结果。"""

    # profile_id稳定标识模式与权重。
    profile_id: str
    # dataset区分开发和锁定结果。
    dataset: Literal["development", "holdout"]
    # reranker说明保持原序还是BM25融合。
    reranker: Literal["off", "bm25"]
    # lexical_weight关闭模式为0，BM25模式记录实际权重。
    lexical_weight: float = Field(ge=0.0, le=1.0)
    # metrics保存统一排序指标和逐样本结果。
    metrics: RAGEvaluationSummary
    # top_1_gain相对同数据集原始顺序的绝对提升。
    top_1_gain: float = Field(ge=-1.0, le=1.0)
    # candidate_set_violation_case_ids必须为空，证明没有新增或删除候选文档。
    candidate_set_violation_case_ids: list[str]
    # evaluation_duration_ms只包含全部search，不包含建库。
    evaluation_duration_ms: float = Field(ge=0.0)
    # mean_case_latency_ms是本机离线近似，不代表生产SLA。
    mean_case_latency_ms: float = Field(ge=0.0)
    # quality_gate_passed表示绝对与相对条件均满足。
    quality_gate_passed: bool
    # quality_gate_failures保存稳定原因码。
    quality_gate_failures: list[str]


class RAGRerankExperimentReport(BaseModel):
    """开发权重扫描、冻结匹配和可选holdout的完整报告。"""

    # experiment_id和version定位实验契约。
    experiment_id: str
    # experiment_version复制配置版本。
    experiment_version: str
    # generated_at使用UTC。
    generated_at: datetime
    # paid_api_called固定False，BM25完全本地计算。
    paid_api_called: Literal[False] = False
    # development_results第一个为Baseline，其余为权重候选。
    development_results: list[RAGRerankProfileResult]
    # selected_profile_id只由开发集选择。
    selected_profile_id: str | None
    # frozen_profile_matches_selection控制holdout权限。
    frozen_profile_matches_selection: bool
    # holdout_baseline用于计算同一锁定集相对提升。
    holdout_baseline: RAGRerankProfileResult | None = None
    # holdout_candidate是冻结候选最终结果。
    holdout_candidate: RAGRerankProfileResult | None = None


def load_rag_rerank_experiment_config(path: Path) -> RAGRerankExperimentConfig:
    """读取并校验排序实验JSON契约。"""

    # 明确UTF-8读取路径和未来中文描述。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic校验全部参数边界。
    return RAGRerankExperimentConfig.model_validate_json(raw_json)


def _build_vector_retriever(config: RAGRerankExperimentConfig) -> KnowledgeRetriever:
    """构建关闭重排但保留范围门的离线Hash/Qdrant检索器。"""

    # settings固定除重排外的全部变量。
    settings = Settings(
        # 不调用聊天模型。
        llm_backend="mock",
        # 不调用生成模型。
        rag_generation_backend="extractive",
        # 使用第24步Hash基线。
        embedding_backend="hash",
        # 维度固定。
        embedding_dimensions=config.embedding_dimensions,
        # 每个Profile使用全新内存索引。
        qdrant_location=":memory:",
        # Collection名称固定且客户端独立，不发生碰撞。
        qdrant_collection="rag_rerank_vector_baseline",
        # 使用困难语料。
        knowledge_source_path=config.corpus_path,
        # 保持晋级阈值。
        rag_score_threshold=config.score_threshold,
        # 当前工厂关闭尚未晋级的默认重排。
        rag_reranker="off",
        # 范围门在外层显式包装。
        rag_query_policy="off",
        # 评测Top-K固定。
        rag_top_k=config.top_k,
        # 切片大小固定。
        rag_chunk_size=config.chunk_size,
        # 重叠固定。
        rag_chunk_overlap=config.chunk_overlap,
    )
    # raw_retriever完成受治理知识建库。
    raw_retriever = build_default_knowledge_retriever(settings)
    # 外层保留第25步晋级范围门。
    return PolicyFilteredKnowledgeRetriever(
        # 允许售后问题继续向量检索。
        retriever=raw_retriever,
        # 使用确定性v1策略。
        query_policy=create_knowledge_query_policy("deterministic_v1"),
    )


def _measure(
    retriever: KnowledgeRetriever,
    cases: list[RAGEvaluationCase],
    *,
    top_k: int,
) -> tuple[RAGEvaluationSummary, float]:
    """运行统一评测并返回指标与纯查询阶段毫秒耗时。"""

    # start在第一条search前记录。
    start = perf_counter()
    # metrics执行全部检索与排序指标计算。
    metrics = evaluate_retriever(retriever, cases, top_k=top_k)
    # duration转换为毫秒。
    duration = (perf_counter() - start) * 1000.0
    # 返回两个结果供Profile构造。
    return metrics, duration


def _candidate_set_violations(
    baseline: RAGEvaluationSummary,
    candidate: RAGEvaluationSummary,
) -> list[str]:
    """检查每条样本重排前后的候选文档集合是否相同。"""

    # candidate_by_id让比较不依赖报告列表位置。
    candidate_by_id = {result.case_id: result for result in candidate.results}
    # violations按Baseline原始顺序保存。
    return [
        # 保存发生集合变化的样本ID。
        result.case_id
        # 遍历每条Baseline结果。
        for result in baseline.results
        # 文档ID集合必须完全相同，顺序允许改变。
        if set(result.retrieved_document_ids)
        != set(candidate_by_id[result.case_id].retrieved_document_ids)
    ]


def _gate_failures(
    metrics: RAGEvaluationSummary,
    *,
    top_1_gain: float,
    violations: list[str],
    gate: RAGRerankQualityGate,
) -> list[str]:
    """计算排序候选绝对质量、相对提升与候选闭包失败原因。"""

    # failures保持稳定检查顺序。
    failures: list[str] = []
    # Recall不足说明候选被错误丢失。
    if metrics.recall_at_k < gate.min_recall_at_k:
        # 追加Recall原因码。
        failures.append("recall_at_k_below_threshold")
    # Top-1绝对质量不足不能晋级。
    if metrics.top_1_accuracy < gate.min_top_1_accuracy:
        # 追加Top-1原因码。
        failures.append("top_1_accuracy_below_threshold")
    # MRR不足说明整体排名改善不够稳定。
    if metrics.mrr_at_k < gate.min_mrr_at_k:
        # 追加MRR原因码。
        failures.append("mrr_at_k_below_threshold")
    # 相对Baseline提升不足时拒绝为了技术名词增加复杂度。
    if top_1_gain + 1e-12 < gate.min_top_1_gain:
        # 追加相对提升原因码。
        failures.append("top_1_gain_below_threshold")
    # 任意候选集合变化都说明实验不再是纯排序单变量。
    if violations:
        # 追加候选闭包原因码。
        failures.append("candidate_set_changed")
    # 返回可能为空的失败列表。
    return failures


def _profile_result(
    *,
    profile_id: str,
    dataset: Literal["development", "holdout"],
    reranker: Literal["off", "bm25"],
    lexical_weight: float,
    metrics: RAGEvaluationSummary,
    baseline_metrics: RAGEvaluationSummary,
    duration_ms: float,
    violations: list[str],
    gate: RAGRerankQualityGate,
) -> RAGRerankProfileResult:
    """把统一指标转换为带相对提升和质量门的Profile结果。"""

    # gain使用同一数据集Baseline计算绝对百分点。
    gain = metrics.top_1_accuracy - baseline_metrics.top_1_accuracy
    # Baseline本身不应被相对提升门判失败，调用方会给它独立结果。
    failures = _gate_failures(
        # 传入当前指标。
        metrics,
        # 传入相对Top-1提升。
        top_1_gain=gain,
        # 传入候选集合违规ID。
        violations=violations,
        # 传入当前数据集门槛。
        gate=gate,
    )
    # 返回强类型结果。
    return RAGRerankProfileResult(
        # 保存Profile名称。
        profile_id=profile_id,
        # 保存数据集类型。
        dataset=dataset,
        # 保存重排模式。
        reranker=reranker,
        # 保存词面权重。
        lexical_weight=lexical_weight,
        # 保存统一指标。
        metrics=metrics,
        # 保存绝对Top-1提升。
        top_1_gain=gain,
        # 保存候选闭包违规。
        candidate_set_violation_case_ids=violations,
        # 保存总查询耗时。
        evaluation_duration_ms=duration_ms,
        # 保存本机平均查询耗时。
        mean_case_latency_ms=duration_ms / metrics.total_cases,
        # 没有失败原因即通过。
        quality_gate_passed=not failures,
        # 保存稳定原因码。
        quality_gate_failures=failures,
    )


def _evaluate_dataset(
    config: RAGRerankExperimentConfig,
    *,
    cases: list[RAGEvaluationCase],
    dataset: Literal["development", "holdout"],
    gate: RAGRerankQualityGate,
) -> list[RAGRerankProfileResult]:
    """在同一数据集运行原序Baseline和全部BM25权重候选。"""

    # baseline_retriever保持原Qdrant顺序。
    baseline_retriever = _build_vector_retriever(config)
    # 测量Baseline指标和查询耗时。
    baseline_metrics, baseline_duration = _measure(
        # 传入原序检索器。
        baseline_retriever,
        # 传入当前数据集。
        cases,
        # 使用固定Top-K。
        top_k=config.top_k,
    )
    # Baseline只用于比较，不要求自身达到相对提升门。
    baseline_result = RAGRerankProfileResult(
        # 稳定名称。
        profile_id="vector-order-baseline",
        # 当前数据集类型。
        dataset=dataset,
        # 明确关闭重排。
        reranker="off",
        # 原序没有词面融合权重。
        lexical_weight=0.0,
        # 保存原始指标。
        metrics=baseline_metrics,
        # 相对自身提升为零。
        top_1_gain=0.0,
        # Baseline没有候选集合比较违规。
        candidate_set_violation_case_ids=[],
        # 保存耗时。
        evaluation_duration_ms=baseline_duration,
        # 保存平均耗时。
        mean_case_latency_ms=baseline_duration / len(cases),
        # Baseline不是候选晋级对象，固定False避免误选。
        quality_gate_passed=False,
        # 稳定说明它只是对照组。
        quality_gate_failures=["baseline_reference_only"],
    )
    # results第一个固定为Baseline。
    results = [baseline_result]
    # 逐个词面权重构建完全独立的原始索引与重排器。
    for weight in config.lexical_weight_candidates:
        # 每个候选重新创建原序检索器，避免共享可变状态。
        vector_retriever = _build_vector_retriever(config)
        # reranking_retriever只重新排列固定候选池。
        reranking_retriever = RerankingKnowledgeRetriever(
            # 注入保持相同阈值和范围门的检索器。
            retriever=vector_retriever,
            # 当前唯一变量是词面融合权重。
            reranker=BM25CandidateReranker(lexical_weight=weight),
            # 固定原候选池大小。
            candidate_k=config.candidate_k,
        )
        # 执行候选评测。
        candidate_metrics, candidate_duration = _measure(
            # 传入重排装饰器。
            reranking_retriever,
            # 使用同一当前数据集。
            cases,
            # 最终Top-K不变。
            top_k=config.top_k,
        )
        # 比较文档集合，证明只改顺序。
        violations = _candidate_set_violations(baseline_metrics, candidate_metrics)
        # 保存当前候选结果。
        results.append(
            _profile_result(
                # 两位小数名称与配置冻结值一致。
                profile_id=f"bm25-fusion-{weight:.2f}",
                # 保存数据集类型。
                dataset=dataset,
                # 明确BM25候选。
                reranker="bm25",
                # 保存当前权重。
                lexical_weight=weight,
                # 保存当前指标。
                metrics=candidate_metrics,
                # 相对同数据集Baseline计算。
                baseline_metrics=baseline_metrics,
                # 保存查询耗时。
                duration_ms=candidate_duration,
                # 保存候选闭包检查。
                violations=violations,
                # 使用当前数据集门槛。
                gate=gate,
            )
        )
    # 返回Baseline与全部候选。
    return results


def run_rag_rerank_experiment(
    config: RAGRerankExperimentConfig,
    *,
    include_holdout: bool = False,
) -> RAGRerankExperimentReport:
    """用开发集选择权重，并在显式确认且冻结匹配时运行holdout。"""

    # development_cases允许反复用于权重选择。
    development_cases = load_rag_evaluation_cases(
        # 从项目根解析开发集。
        resolve_project_path(config.development_dataset_path)
    )
    # 运行开发Baseline和全部权重。
    development_results = _evaluate_dataset(
        # 传入版本化配置。
        config,
        # 传入开发集。
        cases=development_cases,
        # 标记development。
        dataset="development",
        # 使用开发门槛。
        gate=config.development_gate,
    )
    # eligible_results排除Baseline并保留通过候选。
    eligible_results = [
        # 保留当前通过候选。
        result
        # 跳过第一个Baseline。
        for result in development_results[1:]
        # 必须通过全部门槛。
        if result.quality_gate_passed
    ]
    # 优先Top-1、MRR、nDCG；完全同分时选择较低词面权重，减少对向量语义的覆盖。
    selected_result = (
        max(
            eligible_results,
            key=lambda result: (
                # 首要目标是第一名准确率。
                result.metrics.top_1_accuracy,
                # 次要观察首个相关排名。
                result.metrics.mrr_at_k,
                # 再观察整体相关排名质量。
                result.metrics.ndcg_at_k,
                # 同分时低权重用负值参与max。
                -result.lexical_weight,
            ),
        )
        if eligible_results
        else None
    )
    # selected_profile_id只来自开发结果。
    selected_profile_id = selected_result.profile_id if selected_result else None
    # frozen_match防止根据holdout临时换权重。
    frozen_match = selected_profile_id == config.frozen_candidate_profile_id
    # 默认不运行任何holdout。
    holdout_baseline: RAGRerankProfileResult | None = None
    # 默认不生成holdout候选结果。
    holdout_candidate: RAGRerankProfileResult | None = None
    # 只有显式确认才进入锁定路径。
    if include_holdout:
        # 开发优胜者必须已写入冻结配置。
        if selected_result is None or not frozen_match:
            # 在读取和搜索holdout前停止。
            raise ValueError("排序开发优胜者与冻结Profile不一致，禁止运行holdout")
        # 此时才加载此前未运行的排序锁定集。
        holdout_cases = load_rag_evaluation_cases(
            # 从项目根解析锁定数据。
            resolve_project_path(config.holdout_dataset_path)
        )
        # 为公平计算相对提升，运行同一holdout的Baseline与全部候选。
        holdout_results = _evaluate_dataset(
            # 使用相同配置。
            config,
            # 传入排序锁定集。
            cases=holdout_cases,
            # 标记holdout。
            dataset="holdout",
            # 使用预先声明的holdout门槛。
            gate=config.holdout_gate,
        )
        # 第一个结果固定为锁定Baseline。
        holdout_baseline = holdout_results[0]
        # 只公开冻结候选结果，不根据其他holdout权重重新选型。
        holdout_candidate = next(
            # 返回名称与冻结候选一致的结果。
            result
            # 遍历holdout候选。
            for result in holdout_results[1:]
            # 只选择冻结名称。
            if result.profile_id == config.frozen_candidate_profile_id
        )

    # 返回完整实验报告。
    return RAGRerankExperimentReport(
        # 复制实验ID。
        experiment_id=config.experiment_id,
        # 复制版本。
        experiment_version=config.version,
        # 记录UTC时间。
        generated_at=datetime.now(UTC),
        # 本步完全离线。
        paid_api_called=False,
        # 保存开发结果。
        development_results=development_results,
        # 保存开发优胜名称。
        selected_profile_id=selected_profile_id,
        # 保存冻结匹配状态。
        frozen_profile_matches_selection=frozen_match,
        # 可选保存holdout对照。
        holdout_baseline=holdout_baseline,
        # 可选保存冻结候选holdout结果。
        holdout_candidate=holdout_candidate,
    )
