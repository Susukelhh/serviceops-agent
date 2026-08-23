"""第31步：用同一语料公平比较四种检索路线，并冻结 RRF 参数。"""

# datetime 记录报告生成时间；perf_counter 只测本机查询阶段的相对耗时。
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal

# Pydantic 让实验参数、指标和报告都能在运行前完成边界检查。
from pydantic import BaseModel, Field

# 项目路径解析保证 PyCharm 与命令行从不同工作目录启动时读取同一文件。
from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.config.settings import Settings
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository
from serviceops_agent.rag.chunking import KnowledgeChunker
from serviceops_agent.rag.hybrid import BM25CorpusRetriever
from serviceops_agent.rag.query_policy import (
    PolicyFilteredKnowledgeRetriever,
    create_knowledge_query_policy,
)
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    build_default_knowledge_retriever,
)

# ProfileMode 是报告中允许出现的四条检索路线，不接受任意拼写。
type ProfileMode = Literal["dense_only", "candidate_bm25", "lexical_only", "hybrid_rrf"]
# DatasetKind 明确区分可反复调参的开发集和只能在冻结后运行的锁定集。
type DatasetKind = Literal["development", "holdout"]


class RAGHybridQualityGate(BaseModel):
    """RRF 候选必须同时达到的绝对质量和相对改进条件。"""

    # min_recall_at_k 防止为了首位排序而丢掉正确文档。
    min_recall_at_k: float = Field(ge=0.0, le=1.0)
    # min_top_1_accuracy 要求最先交给回答器的文档足够准确。
    min_top_1_accuracy: float = Field(ge=0.0, le=1.0)
    # min_mrr_at_k 约束正确文档不能长期排在候选尾部。
    min_mrr_at_k: float = Field(ge=0.0, le=1.0)
    # min_top_1_gain_vs_dense 要求相对纯向量路线有可量化提升。
    min_top_1_gain_vs_dense: float = Field(ge=-1.0, le=1.0)
    # min_mrr_gain_vs_dense 避免只碰巧修正一条 Top-1。
    min_mrr_gain_vs_dense: float = Field(ge=-1.0, le=1.0)
    # max_false_positive_rate 限制域外问题被错误召回证据的比例。
    max_false_positive_rate: float = Field(ge=0.0, le=1.0)
    # max_ranking_regression_cases 限制被融合后反而排得更后的正例数量。
    max_ranking_regression_cases: int = Field(ge=0, le=100)


class RAGHybridExperimentConfig(BaseModel):
    """版本化的四路检索对照、候选参数和锁定门契约。"""

    # experiment_id 是报告和面试讲解使用的稳定实验名。
    experiment_id: str = Field(min_length=1, max_length=100)
    # version 在数据、公式或质量门变化时递增。
    version: str = Field(min_length=1, max_length=50)
    # corpus_path 指向与生产同结构的公开、受治理知识文档。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_paths 可复用已经揭晓的历史集扩大开发样本，而不冒充新 holdout。
    development_dataset_paths: list[str] = Field(min_length=1, max_length=10)
    # holdout_dataset_path 是本实验新建且默认不读取的锁定集。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_case_count 让默认开发运行只检查文件数量，不解析题目内容。
    holdout_case_count: int = Field(ge=1, le=200)
    # embedding_dimensions 固定离线 Hash 基线向量维度。
    embedding_dimensions: int = Field(ge=64, le=2048)
    # chunk_size 固定单切片字符窗口，当前实验不同时调切片参数。
    chunk_size: int = Field(ge=100, le=2000)
    # chunk_overlap 固定相邻切片重叠字符数。
    chunk_overlap: int = Field(ge=0, le=500)
    # score_threshold 固定向量通道的最低余弦分数。
    score_threshold: float = Field(ge=0.0, le=1.0)
    # top_k 是四条路线最终返回给指标计算的文档数量。
    top_k: int = Field(ge=1, le=20)
    # candidate_k 是旧版“先向量召回、再候选内重排”的候选池大小。
    candidate_k: int = Field(ge=1, le=20)
    # candidate_bm25_weight 固定第26步已晋级的旧版 BM25 权重。
    candidate_bm25_weight: float = Field(ge=0.0, le=1.0)
    # dense_k 是完整混合路线从 Qdrant 独立取回的切片数。
    dense_k: int = Field(ge=1, le=50)
    # lexical_k 是完整混合路线从全语料 BM25 独立取回的切片数。
    lexical_k: int = Field(ge=1, le=50)
    # rrf_k_candidates 扫描 RRF 名次平滑常数。
    rrf_k_candidates: list[int] = Field(min_length=1, max_length=10)
    # dense_weight 固定语义通道贡献，只扫描关键词相对权重。
    dense_weight: float = Field(gt=0.0, le=10.0)
    # lexical_weight_candidates 是开发阶段允许比较的关键词权重列表。
    lexical_weight_candidates: list[float] = Field(min_length=2, max_length=20)
    # frozen_candidate_profile_id 必须由开发优胜者人工写回后才允许运行 holdout。
    frozen_candidate_profile_id: str = Field(min_length=1, max_length=100)
    # development_gate 控制开发集候选是否有资格被选择。
    development_gate: RAGHybridQualityGate
    # holdout_gate 是冻结后泛化验收的独立门槛。
    holdout_gate: RAGHybridQualityGate


class RAGHybridProfileResult(BaseModel):
    """某条检索路线在一个数据集上的完整、可审计结果。"""

    # profile_id 同时编码路线与候选参数。
    profile_id: str
    # dataset 表示指标来自开发集还是锁定集。
    dataset: DatasetKind
    # mode 表示纯向量、旧候选重排、纯关键词或完整 RRF。
    mode: ProfileMode
    # rrf_k 只有完整混合路线有值，其他路线为 None。
    rrf_k: int | None = None
    # lexical_weight 只有涉及 BM25 权重的路线才有值。
    lexical_weight: float | None = None
    # metrics 复用全项目统一的 Recall、Top-1、MRR、nDCG、Decision 与 FPR。
    metrics: RAGEvaluationSummary
    # top_1_gain_vs_dense 是相对同数据集纯向量基线的绝对百分点变化。
    top_1_gain_vs_dense: float = Field(ge=-1.0, le=1.0)
    # mrr_gain_vs_dense 是相对同数据集纯向量基线的 MRR 变化。
    mrr_gain_vs_dense: float = Field(ge=-1.0, le=1.0)
    # ranking_lift_case_ids 保存融合后正确文档排名前移的正例。
    ranking_lift_case_ids: list[str]
    # ranking_regression_case_ids 保存融合后正确文档排名后退或丢失的正例。
    ranking_regression_case_ids: list[str]
    # lexical_rescue_case_ids 表示纯向量 Top-K 漏掉、纯 BM25 找回的正例。
    lexical_rescue_case_ids: list[str]
    # evaluation_duration_ms 是本机整批查询近似耗时，只用于同机相对比较。
    evaluation_duration_ms: float = Field(ge=0.0)
    # mean_case_latency_ms 是总查询耗时除以样本数，不冒充线上 SLA。
    mean_case_latency_ms: float = Field(ge=0.0)
    # quality_gate_passed 只对 RRF 候选有晋级含义；参考路线固定为 False。
    quality_gate_passed: bool
    # quality_gate_failures 保存稳定原因码，便于 CI 和面试复盘。
    quality_gate_failures: list[str]


class RAGHybridExperimentReport(BaseModel):
    """四路对照、参数选择、冻结匹配和可选锁定验收报告。"""

    experiment_id: str
    experiment_version: str
    generated_at: datetime
    # paid_api_called 固定为 False：本步先隔离检索结构，不调用千问。
    paid_api_called: Literal[False] = False
    # development_case_count 公开样本规模，避免只展示百分比。
    development_case_count: int = Field(ge=1)
    # development_results 包含三条参考路线和全部 RRF 参数候选。
    development_results: list[RAGHybridProfileResult]
    # selected_profile_id 只能从开发集通过质量门的 RRF 候选中产生。
    selected_profile_id: str | None
    # frozen_profile_matches_selection 防止看到 holdout 后临时换参数。
    frozen_profile_matches_selection: bool
    # dense_lexical_union_recall_at_k 衡量两条独立召回榜合并后的理论覆盖率。
    dense_lexical_union_recall_at_k: float = Field(ge=0.0, le=1.0)
    # lexical_rescue_case_ids 让“扩大召回池是否救回漏召回”有逐题证据。
    lexical_rescue_case_ids: list[str]
    # holdout_results 默认 None，只有显式确认且冻结匹配时才产生。
    holdout_results: list[RAGHybridProfileResult] | None = None
    # holdout_candidate 保存最终冻结候选，便于命令行直接判断 Gate。
    holdout_candidate: RAGHybridProfileResult | None = None


def load_rag_hybrid_experiment_config(path: Path) -> RAGHybridExperimentConfig:
    """从 UTF-8 JSON 读取并校验第31步实验契约。"""

    return RAGHybridExperimentConfig.model_validate_json(path.read_text(encoding="utf-8"))


def _load_development_cases(config: RAGHybridExperimentConfig) -> list[RAGEvaluationCase]:
    """合并全部已揭晓开发/历史回归集，并拒绝重复 Case ID。"""

    cases: list[RAGEvaluationCase] = []
    for dataset_path in config.development_dataset_paths:
        cases.extend(load_rag_evaluation_cases(resolve_project_path(dataset_path)))
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("混合召回开发集存在重复 case_id")
    return cases


def _base_settings(
    config: RAGHybridExperimentConfig,
    *,
    mode: Literal["off", "bm25", "hybrid_rrf"],
) -> Settings:
    """冻结除检索路线外的共同变量，确保对照只改变一个研究对象。"""

    return Settings(
        llm_backend="mock",
        rag_generation_backend="extractive",
        embedding_backend="hash",
        embedding_dimensions=config.embedding_dimensions,
        qdrant_location=":memory:",
        qdrant_collection=f"rag_hybrid_{mode}",
        knowledge_source_path=config.corpus_path,
        rag_score_threshold=config.score_threshold,
        rag_reranker=mode,
        rag_query_policy="off",
        rag_top_k=config.top_k,
        rag_chunk_size=config.chunk_size,
        rag_chunk_overlap=config.chunk_overlap,
        rag_rerank_candidate_k=config.candidate_k,
        rag_rerank_lexical_weight=config.candidate_bm25_weight,
        rag_hybrid_dense_k=config.dense_k,
        rag_hybrid_lexical_k=config.lexical_k,
        rag_hybrid_rrf_k=config.rrf_k_candidates[0],
        rag_hybrid_dense_weight=config.dense_weight,
        rag_hybrid_lexical_weight=config.lexical_weight_candidates[0],
    )


def _with_scope_policy(retriever: KnowledgeRetriever) -> KnowledgeRetriever:
    """四条路线统一套用第25步已晋级范围门，避免负例处理变量混入实验。"""

    return PolicyFilteredKnowledgeRetriever(
        retriever=retriever,
        query_policy=create_knowledge_query_policy("deterministic_v1"),
    )


def _build_factory_retriever(
    config: RAGHybridExperimentConfig,
    *,
    mode: Literal["off", "bm25", "hybrid_rrf"],
    rrf_k: int | None = None,
    lexical_weight: float | None = None,
) -> KnowledgeRetriever:
    """通过线上同一装配工厂构建纯向量、旧重排或完整 RRF 路线。"""

    settings = _base_settings(config, mode=mode)
    if rrf_k is not None:
        settings.rag_hybrid_rrf_k = rrf_k
    if lexical_weight is not None:
        settings.rag_hybrid_lexical_weight = lexical_weight
    return _with_scope_policy(build_default_knowledge_retriever(settings))


def _build_lexical_retriever(config: RAGHybridExperimentConfig) -> KnowledgeRetriever:
    """用相同治理语料和切片参数建立独立全库 BM25 参考路线。"""

    repository = JsonKnowledgeRepository(resolve_project_path(config.corpus_path))
    documents = repository.list_indexable_documents()
    chunks = KnowledgeChunker(
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
    ).split_documents(documents)
    return _with_scope_policy(BM25CorpusRetriever(chunks=chunks))


def _measure(
    retriever: KnowledgeRetriever,
    cases: list[RAGEvaluationCase],
    *,
    top_k: int,
) -> tuple[RAGEvaluationSummary, float]:
    """执行统一指标计算并返回本机查询阶段耗时。"""

    started_at = perf_counter()
    metrics = evaluate_retriever(retriever, cases, top_k=top_k)
    return metrics, (perf_counter() - started_at) * 1000.0


def _rank_changes(
    baseline: RAGEvaluationSummary,
    candidate: RAGEvaluationSummary,
) -> tuple[list[str], list[str]]:
    """逐题比较正确文档排名，返回前移和后退的 Case ID。"""

    baseline_by_id = {result.case_id: result for result in baseline.results}
    lifted: list[str] = []
    regressed: list[str] = []
    for result in candidate.results:
        baseline_result = baseline_by_id[result.case_id]
        if not result.should_retrieve:
            continue
        old_rank = baseline_result.first_relevant_rank or 10**9
        new_rank = result.first_relevant_rank or 10**9
        if new_rank < old_rank:
            lifted.append(result.case_id)
        elif new_rank > old_rank:
            regressed.append(result.case_id)
    return lifted, regressed


def _lexical_rescues(
    dense: RAGEvaluationSummary,
    lexical: RAGEvaluationSummary,
) -> list[str]:
    """找出向量 Top-K 未召回、关键词 Top-K 成功召回的正例。"""

    lexical_by_id = {result.case_id: result for result in lexical.results}
    return [
        result.case_id
        for result in dense.results
        if result.should_retrieve
        and not result.passed
        and lexical_by_id[result.case_id].passed
    ]


def _union_recall(
    dense: RAGEvaluationSummary,
    lexical: RAGEvaluationSummary,
) -> float:
    """计算两路 Top-K 中任一路命中期望文档的正例比例。"""

    lexical_by_id = {result.case_id: result for result in lexical.results}
    union_hits = sum(
        1
        for result in dense.results
        if result.should_retrieve
        and (result.passed or lexical_by_id[result.case_id].passed)
    )
    return union_hits / dense.positive_cases


def _gate_failures(
    metrics: RAGEvaluationSummary,
    *,
    top_1_gain: float,
    mrr_gain: float,
    regression_count: int,
    gate: RAGHybridQualityGate,
) -> list[str]:
    """把质量门翻译为稳定失败原因码。"""

    failures: list[str] = []
    if metrics.recall_at_k < gate.min_recall_at_k:
        failures.append("recall_at_k_below_threshold")
    if metrics.top_1_accuracy < gate.min_top_1_accuracy:
        failures.append("top_1_accuracy_below_threshold")
    if metrics.mrr_at_k < gate.min_mrr_at_k:
        failures.append("mrr_at_k_below_threshold")
    if top_1_gain + 1e-12 < gate.min_top_1_gain_vs_dense:
        failures.append("top_1_gain_vs_dense_below_threshold")
    if mrr_gain + 1e-12 < gate.min_mrr_gain_vs_dense:
        failures.append("mrr_gain_vs_dense_below_threshold")
    if metrics.false_positive_rate > gate.max_false_positive_rate + 1e-12:
        failures.append("false_positive_rate_above_threshold")
    if regression_count > gate.max_ranking_regression_cases:
        failures.append("ranking_regression_cases_above_threshold")
    return failures


def _result(
    *,
    profile_id: str,
    dataset: DatasetKind,
    mode: ProfileMode,
    metrics: RAGEvaluationSummary,
    duration_ms: float,
    dense_metrics: RAGEvaluationSummary,
    lexical_rescues: list[str],
    gate: RAGHybridQualityGate | None,
    rrf_k: int | None = None,
    lexical_weight: float | None = None,
) -> RAGHybridProfileResult:
    """把统一指标补充为带相对变化和质量门的 Profile 结果。"""

    top_1_gain = metrics.top_1_accuracy - dense_metrics.top_1_accuracy
    mrr_gain = metrics.mrr_at_k - dense_metrics.mrr_at_k
    lifted, regressed = _rank_changes(dense_metrics, metrics)
    failures = (
        _gate_failures(
            metrics,
            top_1_gain=top_1_gain,
            mrr_gain=mrr_gain,
            regression_count=len(regressed),
            gate=gate,
        )
        if gate is not None
        else ["reference_profile_only"]
    )
    return RAGHybridProfileResult(
        profile_id=profile_id,
        dataset=dataset,
        mode=mode,
        rrf_k=rrf_k,
        lexical_weight=lexical_weight,
        metrics=metrics,
        top_1_gain_vs_dense=top_1_gain,
        mrr_gain_vs_dense=mrr_gain,
        ranking_lift_case_ids=lifted,
        ranking_regression_case_ids=regressed,
        lexical_rescue_case_ids=lexical_rescues,
        evaluation_duration_ms=duration_ms,
        mean_case_latency_ms=duration_ms / metrics.total_cases,
        quality_gate_passed=not failures,
        quality_gate_failures=failures,
    )


def _evaluate_dataset(
    config: RAGHybridExperimentConfig,
    *,
    cases: list[RAGEvaluationCase],
    dataset: DatasetKind,
    gate: RAGHybridQualityGate,
) -> tuple[list[RAGHybridProfileResult], float, list[str]]:
    """在同一数据集依次运行三条参考路线和全部 RRF 参数候选。"""

    dense_metrics, dense_duration = _measure(
        _build_factory_retriever(config, mode="off"), cases, top_k=config.top_k
    )
    candidate_metrics, candidate_duration = _measure(
        _build_factory_retriever(config, mode="bm25"), cases, top_k=config.top_k
    )
    lexical_metrics, lexical_duration = _measure(
        _build_lexical_retriever(config), cases, top_k=config.top_k
    )
    lexical_rescues = _lexical_rescues(dense_metrics, lexical_metrics)
    union_recall = _union_recall(dense_metrics, lexical_metrics)
    results = [
        _result(
            profile_id="dense-only-baseline",
            dataset=dataset,
            mode="dense_only",
            metrics=dense_metrics,
            duration_ms=dense_duration,
            dense_metrics=dense_metrics,
            lexical_rescues=lexical_rescues,
            gate=None,
        ),
        _result(
            profile_id="legacy-candidate-bm25",
            dataset=dataset,
            mode="candidate_bm25",
            metrics=candidate_metrics,
            duration_ms=candidate_duration,
            dense_metrics=dense_metrics,
            lexical_rescues=lexical_rescues,
            gate=None,
            lexical_weight=config.candidate_bm25_weight,
        ),
        _result(
            profile_id="full-corpus-bm25",
            dataset=dataset,
            mode="lexical_only",
            metrics=lexical_metrics,
            duration_ms=lexical_duration,
            dense_metrics=dense_metrics,
            lexical_rescues=lexical_rescues,
            gate=None,
        ),
    ]
    for rrf_k in config.rrf_k_candidates:
        for lexical_weight in config.lexical_weight_candidates:
            metrics, duration = _measure(
                _build_factory_retriever(
                    config,
                    mode="hybrid_rrf",
                    rrf_k=rrf_k,
                    lexical_weight=lexical_weight,
                ),
                cases,
                top_k=config.top_k,
            )
            results.append(
                _result(
                    profile_id=f"hybrid-rrf-k{rrf_k}-lex{lexical_weight:.2f}",
                    dataset=dataset,
                    mode="hybrid_rrf",
                    metrics=metrics,
                    duration_ms=duration,
                    dense_metrics=dense_metrics,
                    lexical_rescues=lexical_rescues,
                    gate=gate,
                    rrf_k=rrf_k,
                    lexical_weight=lexical_weight,
                )
            )
    return results, union_recall, lexical_rescues


def run_rag_hybrid_experiment(
    config: RAGHybridExperimentConfig,
    *,
    include_holdout: bool = False,
) -> RAGHybridExperimentReport:
    """开发集选择 RRF 参数；仅在显式确认且冻结匹配后运行新 holdout。"""

    development_cases = _load_development_cases(config)
    development_results, union_recall, lexical_rescues = _evaluate_dataset(
        config,
        cases=development_cases,
        dataset="development",
        gate=config.development_gate,
    )
    eligible = [
        result
        for result in development_results
        if result.mode == "hybrid_rrf" and result.quality_gate_passed
    ]
    selected = (
        max(
            eligible,
            key=lambda result: (
                result.metrics.top_1_accuracy,
                result.metrics.mrr_at_k,
                result.metrics.ndcg_at_k,
                -len(result.ranking_regression_case_ids),
                -(result.lexical_weight or 0.0),
                # 指标完全同分时保留较大的平滑常数；60 是当前线上工程默认值，减少无收益改动。
                result.rrf_k or 0,
            ),
        )
        if eligible
        else None
    )
    selected_profile_id = selected.profile_id if selected is not None else None
    frozen_matches = selected_profile_id == config.frozen_candidate_profile_id
    holdout_results: list[RAGHybridProfileResult] | None = None
    holdout_candidate: RAGHybridProfileResult | None = None
    if include_holdout:
        if selected is None or not frozen_matches:
            raise ValueError("RRF 开发优胜者与冻结 Profile 不一致，禁止运行 holdout")
        holdout_cases = load_rag_evaluation_cases(
            resolve_project_path(config.holdout_dataset_path)
        )
        if len(holdout_cases) != config.holdout_case_count:
            raise ValueError("RRF holdout 实际样本数与配置声明不一致")
        holdout_results, _, _ = _evaluate_dataset(
            config,
            cases=holdout_cases,
            dataset="holdout",
            gate=config.holdout_gate,
        )
        holdout_candidate = next(
            result
            for result in holdout_results
            if result.profile_id == config.frozen_candidate_profile_id
        )
    return RAGHybridExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        generated_at=datetime.now(UTC),
        development_case_count=len(development_cases),
        development_results=development_results,
        selected_profile_id=selected_profile_id,
        frozen_profile_matches_selection=frozen_matches,
        dense_lexical_union_recall_at_k=union_recall,
        lexical_rescue_case_ids=lexical_rescues,
        holdout_results=holdout_results,
        holdout_candidate=holdout_candidate,
    )
