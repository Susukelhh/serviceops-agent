"""第25步阈值扫描、FAQ范围门候选和锁定集验收实验。"""

# datetime记录报告UTC时间；perf_counter只测评测搜索阶段耗时。
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

# Literal限制报告中的策略版本和数据集名称。
from typing import Literal

# BaseModel/Field/TypeAdapter校验实验配置、报告和原始知识文档。
from pydantic import BaseModel, Field, TypeAdapter

# resolve_project_path让PyCharm和终端使用同一项目相对路径。
from serviceops_agent.config.paths import resolve_project_path

# Settings显式构建完全离线、互相隔离的Qdrant候选。
from serviceops_agent.config.settings import Settings

# KnowledgeDocument用于在建库前校验实验语料Schema。
from serviceops_agent.domain.knowledge import KnowledgeDocument

# 通用评测器负责计算相同口径的Recall、排序与负例指标。
from serviceops_agent.evaluation.rag_evaluator import (
    RAGEvaluationCase,
    RAGEvaluationSummary,
    evaluate_retriever,
    load_rag_evaluation_cases,
)

# 查询策略工厂和装饰器确保线上与离线候选使用同一实现。
from serviceops_agent.rag.query_policy import (
    PolicyFilteredKnowledgeRetriever,
    QueryPolicyMode,
    create_knowledge_query_policy,
)

# KnowledgeRetriever协议统一裸Qdrant与范围门装饰器的类型。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    build_default_knowledge_retriever,
)


class RAGScopeQualityGate(BaseModel):
    """候选在指定数据集上必须同时满足的质量边界。"""

    # min_recall_at_k防止通过过度拒答把负例变好、正例却全部挡住。
    min_recall_at_k: float = Field(ge=0.0, le=1.0)
    # min_decision_accuracy同时约束正例召回和负例拒绝。
    min_decision_accuracy: float = Field(ge=0.0, le=1.0)
    # max_false_positive_rate限制域外问题错误获得知识证据的比例。
    max_false_positive_rate: float = Field(ge=0.0, le=1.0)


class RAGScopeExperimentConfig(BaseModel):
    """版本化的第25步离线候选与锁定验收契约。"""

    # experiment_id标识同一问题实验系列。
    experiment_id: str = Field(min_length=1, max_length=100)
    # version在数据、策略规则或晋级门变化时递增。
    version: str = Field(min_length=1, max_length=50)
    # corpus_path指向第24步冻结的困难语料。
    corpus_path: str = Field(min_length=1, max_length=500)
    # development_dataset_path允许反复比较候选。
    development_dataset_path: str = Field(min_length=1, max_length=500)
    # holdout_dataset_path只在候选冻结后运行一次。
    holdout_dataset_path: str = Field(min_length=1, max_length=500)
    # embedding_dimensions保持Hash空间与Baseline一致。
    embedding_dimensions: int = Field(default=1024, ge=64, le=2048)
    # chunk_size在本步固定，不与范围策略同时改变。
    chunk_size: int = Field(default=500, ge=100, le=2000)
    # chunk_overlap在本步固定，不把切片变化混进拒答实验。
    chunk_overlap: int = Field(default=80, ge=0, le=500)
    # top_k与第24步保持一致，确保Recall和排序可直接比较。
    top_k: int = Field(default=5, ge=1, le=20)
    # threshold_candidates按声明顺序执行阈值单变量扫描。
    threshold_candidates: list[float] = Field(min_length=2, max_length=20)
    # scope_candidate_threshold让范围门只改变策略，不偷偷改变阈值。
    scope_candidate_threshold: float = Field(ge=0.0, le=1.0)
    # frozen_candidate_profile_id是运行holdout前已经确定的候选名称。
    frozen_candidate_profile_id: str = Field(min_length=1, max_length=100)
    # development_gate用于候选选型。
    development_gate: RAGScopeQualityGate
    # holdout_gate用于最后泛化验收，不能反向参与选型。
    holdout_gate: RAGScopeQualityGate


class RAGScopeProfileResult(BaseModel):
    """一个固定阈值与策略Profile在一份数据集上的结果。"""

    # profile_id包含候选策略和阈值，报告脱离配置仍可解释。
    profile_id: str
    # dataset说明当前结果来自development还是holdout。
    dataset: Literal["development", "holdout"]
    # score_threshold记录Qdrant实际使用的余弦门槛。
    score_threshold: float = Field(ge=0.0, le=1.0)
    # query_policy记录范围门是否关闭或使用v1。
    query_policy: QueryPolicyMode
    # metrics使用与Baseline相同的统一指标对象。
    metrics: RAGEvaluationSummary
    # blocked_case_ids保存范围策略在Embedding前拦截的样本ID。
    blocked_case_ids: list[str]
    # blocked_reason_codes按样本顺序保存有限原因码，便于发现误拒绝。
    blocked_reason_codes: list[str]
    # evaluation_duration_ms只包含全部query search，不包含建库。
    evaluation_duration_ms: float = Field(ge=0.0)
    # mean_case_latency_ms是当前本机离线近似值，不能冒充线上SLA。
    mean_case_latency_ms: float = Field(ge=0.0)
    # quality_gate_passed表示本Profile是否同时满足当前数据集门槛。
    quality_gate_passed: bool
    # quality_gate_failures保存稳定、机器可读的未达标原因。
    quality_gate_failures: list[str]


class RAGScopeExperimentReport(BaseModel):
    """阈值扫描、冻结候选与可选锁定集的完整报告。"""

    # experiment_id和version共同定位实验契约。
    experiment_id: str
    # experiment_version复制配置版本。
    experiment_version: str
    # generated_at使用UTC，便于比较运行顺序。
    generated_at: datetime
    # paid_api_called固定False，本步只使用Hash与确定性规则。
    paid_api_called: Literal[False] = False
    # development_results按阈值扫描顺序保存，最后一个是范围门候选。
    development_results: list[RAGScopeProfileResult]
    # selected_profile_id完全由development结果和质量门选择。
    selected_profile_id: str | None
    # frozen_profile_matches_selection防止运行holdout时临时更换候选。
    frozen_profile_matches_selection: bool
    # holdout_result为None表示本轮遵守纪律，没有查看锁定集结果。
    holdout_result: RAGScopeProfileResult | None = None
    # baseline_false_positive_rate用于计算范围门的核心问题改善。
    baseline_false_positive_rate: float = Field(ge=0.0, le=1.0)
    # selected_false_positive_reduction是Baseline减候选的绝对百分点。
    selected_false_positive_reduction: float | None = Field(default=None, ge=-1.0, le=1.0)
    # selected_decision_accuracy_gain是候选决策准确率的绝对提升。
    selected_decision_accuracy_gain: float | None = Field(default=None, ge=-1.0, le=1.0)


def load_rag_scope_experiment_config(path: Path) -> RAGScopeExperimentConfig:
    """读取并校验第25步版本化实验配置。"""

    # 明确UTF-8读取中文路径或未来描述字段。
    raw_json = path.read_text(encoding="utf-8")
    # Pydantic一次完成所有参数范围和必填字段校验。
    return RAGScopeExperimentConfig.model_validate_json(raw_json)


def _quality_gate_failures(
    metrics: RAGEvaluationSummary,
    gate: RAGScopeQualityGate,
) -> list[str]:
    """使用稳定原因码判断一组检索指标是否达到晋级门。"""

    # failures按配置字段顺序保存，保证报告diff稳定。
    failures: list[str] = []
    # Recall不足说明拒答策略可能误伤知识内问题。
    if metrics.recall_at_k < gate.min_recall_at_k:
        # 追加正例召回失败原因。
        failures.append("recall_at_k_below_threshold")
    # 决策准确率不足说明整体正负判断仍不可靠。
    if metrics.decision_accuracy < gate.min_decision_accuracy:
        # 追加整体决策失败原因。
        failures.append("decision_accuracy_below_threshold")
    # 负例误召回过高说明“什么都敢搜”的核心问题没有解决。
    if metrics.false_positive_rate > gate.max_false_positive_rate:
        # 追加误召回失败原因。
        failures.append("false_positive_rate_above_threshold")
    # 空列表表示三个边界同时满足。
    return failures


def _build_offline_retriever(
    config: RAGScopeExperimentConfig,
    *,
    threshold: float,
    policy_mode: QueryPolicyMode,
    profile_id: str,
) -> KnowledgeRetriever:
    """为单个Profile创建全新离线Qdrant并按需增加范围门。"""

    # 每个Profile使用独立内存Collection，避免上一个阈值的状态影响本轮。
    settings = Settings(
        # 分类与本检索实验无关，但固定mock防止意外模型调用。
        llm_backend="mock",
        # 回答层不参与检索评测，仍固定零费用摘录模式。
        rag_generation_backend="extractive",
        # 本步只研究Hash阈值和范围门。
        embedding_backend="hash",
        # 维度固定来自实验配置。
        embedding_dimensions=config.embedding_dimensions,
        # 内存Qdrant每轮从空索引开始。
        qdrant_location=":memory:",
        # Profile名称转换为合法且易读的Collection名称。
        qdrant_collection="rag_scope_" + profile_id.replace(".", "_").replace("-", "_"),
        # 复用第24步困难语料。
        knowledge_source_path=config.corpus_path,
        # 当前Profile唯一可变的Qdrant分数门槛。
        rag_score_threshold=threshold,
        # 原始检索器不自行使用范围策略；装饰器显式处理，避免重复判断。
        rag_query_policy="off",
        # 第25步必须保留原Qdrant顺序，不能让第26步重排改写历史结果。
        rag_reranker="off",
        # Top-K保持实验契约一致。
        rag_top_k=config.top_k,
        # Chunk参数保持不变。
        rag_chunk_size=config.chunk_size,
        # Overlap参数保持不变。
        rag_chunk_overlap=config.chunk_overlap,
    )
    # raw_retriever完成知识治理、切片、Hash向量化和Qdrant建库。
    raw_retriever = build_default_knowledge_retriever(settings)
    # 关闭策略时直接返回裸检索器，形成阈值单变量候选。
    if policy_mode == "off":
        # 不增加任何前置判断。
        return raw_retriever
    # 范围门候选用装饰器复用线上同一策略。
    return PolicyFilteredKnowledgeRetriever(
        # 允许请求继续进入这个裸Qdrant检索器。
        retriever=raw_retriever,
        # 根据明确Profile创建确定性v1策略。
        query_policy=create_knowledge_query_policy(policy_mode),
    )


def _evaluate_profile(
    config: RAGScopeExperimentConfig,
    *,
    cases: list[RAGEvaluationCase],
    dataset: Literal["development", "holdout"],
    profile_id: str,
    threshold: float,
    policy_mode: QueryPolicyMode,
    gate: RAGScopeQualityGate,
) -> RAGScopeProfileResult:
    """构建并评估一个候选，同时记录范围门拦截样本。"""

    # retriever在计时前完成建库，使延迟只反映查询阶段。
    retriever = _build_offline_retriever(
        # 传入固定实验配置。
        config,
        # 传入当前阈值。
        threshold=threshold,
        # 传入当前策略版本。
        policy_mode=policy_mode,
        # Profile名称用于Collection隔离。
        profile_id=profile_id,
    )
    # policy只用于记录哪些样本在Embedding前被挡住；off模式会全部允许。
    policy = create_knowledge_query_policy(policy_mode)
    # assessments与cases保持严格一一对应顺序。
    assessments = [policy.assess(case.question) for case in cases]
    # blocked_case_ids只保存样本ID，不复制用户问题正文。
    blocked_case_ids = [
        # 复制当前被拒样本ID。
        case.case_id
        # zip strict确保策略判断和样本数量绝不静默错位。
        for case, assessment in zip(cases, assessments, strict=True)
        # 只收集不允许进入检索的样本。
        if not assessment.allowed
    ]
    # blocked_reason_codes使用同一顺序保存原因码。
    blocked_reason_codes = [
        # 复制有限原因码。
        assessment.reason_code
        # 遍历全部策略结论。
        for assessment in assessments
        # 只保存实际拒绝原因。
        if not assessment.allowed
    ]
    # query_started_at从所有search开始前取高精度单调时间。
    query_started_at = perf_counter()
    # metrics对当前数据集执行相同口径评测。
    metrics = evaluate_retriever(
        # 传入裸检索或范围门装饰器。
        retriever,
        # 传入开发集或锁定集。
        cases,
        # Top-K保持配置固定。
        top_k=config.top_k,
    )
    # duration_ms不包含索引构建，转换为毫秒便于阅读。
    duration_ms = (perf_counter() - query_started_at) * 1000.0
    # failures根据当前数据集的独立质量门计算。
    failures = _quality_gate_failures(metrics, gate)
    # 返回完整Profile结果。
    return RAGScopeProfileResult(
        # 写入稳定名称。
        profile_id=profile_id,
        # 写入数据集类型。
        dataset=dataset,
        # 写入真实阈值。
        score_threshold=threshold,
        # 写入策略版本。
        query_policy=policy_mode,
        # 嵌入统一指标与逐样本排名。
        metrics=metrics,
        # 保存范围门拒绝样本。
        blocked_case_ids=blocked_case_ids,
        # 保存对应有限原因码。
        blocked_reason_codes=blocked_reason_codes,
        # 保存本机离线总搜索耗时。
        evaluation_duration_ms=duration_ms,
        # 用总耗时除以样本数得到近似平均值。
        mean_case_latency_ms=duration_ms / len(cases),
        # 没有失败原因即通过当前门槛。
        quality_gate_passed=not failures,
        # 保存稳定失败原因列表。
        quality_gate_failures=failures,
    )


def run_rag_scope_experiment(
    config: RAGScopeExperimentConfig,
    *,
    include_holdout: bool = False,
) -> RAGScopeExperimentReport:
    """运行开发集候选；只有显式请求且冻结候选匹配时才运行holdout。"""

    # 先校验知识语料Schema，避免候选运行中途才发现数据损坏。
    TypeAdapter(list[KnowledgeDocument]).validate_json(
        # 明确UTF-8读取版本化知识文件。
        resolve_project_path(config.corpus_path).read_text(encoding="utf-8")
    )
    # development_cases允许反复运行用于候选选择。
    development_cases = load_rag_evaluation_cases(
        # 从项目根解析开发集路径。
        resolve_project_path(config.development_dataset_path)
    )
    # development_results按配置阈值顺序保存。
    development_results: list[RAGScopeProfileResult] = []
    # 逐个阈值运行关闭范围门的单变量实验。
    for threshold in config.threshold_candidates:
        # 阈值Profile名称保留两位小数，报告容易比较。
        profile_id = f"threshold-only-{threshold:.2f}"
        # 构建、评测并追加当前阈值结果。
        development_results.append(
            _evaluate_profile(
                # 传入固定配置。
                config,
                # 所有阈值都使用同一开发集。
                cases=development_cases,
                # 标记开发集结果。
                dataset="development",
                # 写入当前Profile名称。
                profile_id=profile_id,
                # 只改变当前阈值。
                threshold=threshold,
                # 关闭范围门保证单变量。
                policy_mode="off",
                # 使用开发晋级门。
                gate=config.development_gate,
            )
        )
    # scope_profile_id必须与配置中的冻结名称规则一致。
    scope_profile_id = f"scope-gate-v1-threshold-{config.scope_candidate_threshold:.2f}"
    # 范围门候选保持Baseline阈值，只增加确定性v1策略。
    development_results.append(
        _evaluate_profile(
            # 复用同一配置。
            config,
            # 复用同一开发集。
            cases=development_cases,
            # 标记开发集。
            dataset="development",
            # 写入稳定范围门Profile名称。
            profile_id=scope_profile_id,
            # 阈值来自独立候选字段。
            threshold=config.scope_candidate_threshold,
            # 只增加第一版确定性范围门。
            policy_mode="deterministic_v1",
            # 使用同一开发晋级门。
            gate=config.development_gate,
        )
    )
    # eligible_results只保留同时满足Recall、决策和误召回边界的候选。
    eligible_results = [
        # 保留当前通过结果。
        result
        # 遍历全部阈值和范围门候选。
        for result in development_results
        # 必须通过完整质量门。
        if result.quality_gate_passed
    ]
    # 在通过候选中优先决策准确率，再看Recall、Top-1和更低误召回。
    selected_result = (
        # max使用只来自开发集的确定性排序键。
        max(
            eligible_results,
            key=lambda result: (
                # 首要解决整体正负决策。
                result.metrics.decision_accuracy,
                # 防止过度拒答。
                result.metrics.recall_at_k,
                # 同分时保留更好的第一名证据。
                result.metrics.top_1_accuracy,
                # 更低误召回用负值参与max。
                -result.metrics.false_positive_rate,
            ),
        )
        # 没有候选通过时不强行晋级。
        if eligible_results
        else None
    )
    # selected_profile_id只从开发集选择结果复制。
    selected_profile_id = selected_result.profile_id if selected_result else None
    # frozen_match要求开发集优胜者与运行holdout前写入配置的候选完全一致。
    frozen_match = selected_profile_id == config.frozen_candidate_profile_id
    # baseline_result固定是阈值列表第一项且策略关闭。
    baseline_result = development_results[0]
    # selected_fp_reduction在没有候选时保持None。
    selected_fp_reduction = (
        # 绝对百分点更容易解释“100%降到0%”。
        baseline_result.metrics.false_positive_rate
        - selected_result.metrics.false_positive_rate
        # 没有晋级候选时不计算虚假提升。
        if selected_result
        else None
    )
    # selected_decision_gain同样只在候选存在时计算。
    selected_decision_gain = (
        # 候选减Baseline得到绝对准确率提升。
        selected_result.metrics.decision_accuracy
        - baseline_result.metrics.decision_accuracy
        # 无候选时保持空值。
        if selected_result
        else None
    )
    # holdout_result默认None，开发实验不会偷看锁定集。
    holdout_result: RAGScopeProfileResult | None = None
    # 只有调用方显式确认时才考虑运行锁定集。
    if include_holdout:
        # 冻结名称不匹配说明候选尚未锁定，必须停止而不是临时换Profile。
        if not frozen_match or selected_result is None:
            # 错误不运行任何holdout search。
            raise ValueError("开发集优胜候选与冻结Profile不一致，禁止运行holdout")
        # 此时才读取并校验锁定集。
        holdout_cases = load_rag_evaluation_cases(
            # 从项目根解析锁定集路径。
            resolve_project_path(config.holdout_dataset_path)
        )
        # 使用已经冻结的阈值和策略执行一次锁定验收。
        holdout_result = _evaluate_profile(
            # 复用实验配置。
            config,
            # 传入此前未运行的锁定集。
            cases=holdout_cases,
            # 标记holdout结果。
            dataset="holdout",
            # 名称必须与开发优胜者一致。
            profile_id=selected_result.profile_id,
            # 复制冻结候选阈值。
            threshold=selected_result.score_threshold,
            # 复制冻结候选策略版本。
            policy_mode=selected_result.query_policy,
            # 使用预先声明的独立锁定门槛。
            gate=config.holdout_gate,
        )

    # 返回可以写入runtime的完整实验报告。
    return RAGScopeExperimentReport(
        # 复制实验系列ID。
        experiment_id=config.experiment_id,
        # 复制契约版本。
        experiment_version=config.version,
        # 记录当前UTC时间。
        generated_at=datetime.now(UTC),
        # 本步没有任何真实模型调用。
        paid_api_called=False,
        # 保存全部开发候选。
        development_results=development_results,
        # 保存只由开发集确定的优胜名称。
        selected_profile_id=selected_profile_id,
        # 保存冻结匹配状态。
        frozen_profile_matches_selection=frozen_match,
        # 可选保存锁定验收结果。
        holdout_result=holdout_result,
        # 保存Baseline误召回率。
        baseline_false_positive_rate=baseline_result.metrics.false_positive_rate,
        # 保存候选误召回绝对改善。
        selected_false_positive_reduction=selected_fp_reduction,
        # 保存候选决策准确率绝对改善。
        selected_decision_accuracy_gain=selected_decision_gain,
    )
