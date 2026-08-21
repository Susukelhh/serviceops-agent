"""真实千问候选模型的重复端到端实验、稳定性聚合和晋级质量门。

本模块把“确定性基线是否仍然正确”和“外部模型候选是否足够稳定”分成两类证据。
候选实验只使用人工合成的黄金集，不读取生产会话；API Key 只进入 LangChain 客户端，
不会写入报告、日志或 LangGraph State。
"""

# Callable 表达每轮实验都必须重新构造图、Checkpointer 和副作用仓库的目标工厂。
from collections.abc import Callable

# UTC/datetime 为实验报告写入可比较的带时区生成时间。
from datetime import UTC, datetime

# fmean 计算多轮实验算术平均值，避免手写除法分母错误。
from statistics import fmean

# uuid4 让每轮真实模型实验使用隔离的内存 Qdrant Collection。
from uuid import uuid4

# InMemorySaver 保证候选评测不会读取或污染本地持久化审批线程。
from langgraph.checkpoint.memory import InMemorySaver

# BaseModel/Field/TypeAdapter 为实验配置、稳定性结果和最终报告提供强类型边界。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# 真实候选使用 LangChain 结构化工具规划器；每个动作仍受执行层白名单约束。
from serviceops_agent.agent.planner import create_tool_planner

# resolve_project_path 让 PyCharm 和 CI 从任意工作目录读取同一配置文件。
from serviceops_agent.config.paths import resolve_project_path

# Settings 提供千问模型、密钥和 OpenAI 兼容 Base URL；密钥字段使用 SecretStr。
from serviceops_agent.config.settings import Settings

# Intent 用于从黄金标签估算预期模型调用量，不依赖模型实际路由结果。
from serviceops_agent.domain.enums import Intent

# 第十三步的单轮数据模型和评测器是重复实验的基础单元。
from serviceops_agent.evaluation.agent_evaluator import (
    AgentEvaluationDataset,
    AgentEvaluationSummary,
    build_offline_agent_evaluation_target,
    evaluate_agent_dataset,
)

# 整图工厂复用 FastAPI 相同节点和边，只替换运行时依赖。
from serviceops_agent.graph.builder import ServiceGraph, build_service_graph

# 默认订单仓库提供无真实客户信息的受控种子数据。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 每轮独立退货仓库用于验证审批前业务零写入。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    ReturnRequestRepository,
)

# 真实分类节点工厂把千问绑定到有限 IntentClassification Schema。
from serviceops_agent.llm.factory import build_intent_classifier_node

# 真实 FAQ 回答工厂把千问绑定到 GroundedAnswerDraft Schema 和引用白名单后置校验。
from serviceops_agent.rag.generation import create_grounded_answer_client

# 知识检索仍固定为 Hash Embedding + 内存 Qdrant，以单独观察聊天模型变量。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever

# qwen_chat_hash_retrieval 表示聊天决策使用真实千问、检索仍使用确定性 Hash 基线。
QWEN_CANDIDATE_PROFILE = "qwen_chat_hash_retrieval"

# EvaluationTargetFactory 规定每次调用都返回相互绑定的图和副作用仓库。
type EvaluationTargetFactory = Callable[
    [],
    tuple[ServiceGraph, ReturnRequestRepository],
]


class CandidatePromotionThresholds(BaseModel):
    """真实模型候选相对离线基线进入下一阶段前必须达到的聚合门。"""

    # 基线不过说明代码本身已回归，此时没有资格评价外部模型候选。
    require_baseline_gate_passed: bool = True
    # 多轮整体平均值要求候选在大多数样本上持续正确，而非只看最好一轮。
    min_mean_overall_pass_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    # 最差一轮限制模型偶发大面积退化，避免平均值掩盖灾难性波动。
    min_worst_trial_overall_pass_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    # fully_stable 指一条用例在所有轮次都通过；该比例直接衡量逐场景稳定性。
    min_fully_stable_case_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    # 安全维度默认要求 100%，业务体验可调，但越权或审批前写入不能靠平均值容忍。
    min_mean_safety_invariant_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_rate_relationships(self) -> "CandidatePromotionThresholds":
        """拒绝最差轮门槛高于平均门槛的反直觉实验配置。"""

        # worst 不可能高于 mean；门槛关系保持同样方向更便于解释和调参。
        if self.min_worst_trial_overall_pass_rate > self.min_mean_overall_pass_rate:
            raise ValueError("最差轮通过率门槛不能高于平均通过率门槛")
        # 返回已完成跨字段校验的晋级阈值。
        return self


class AgentCandidateExperimentConfig(BaseModel):
    """受 Git 版本控制的候选实验身份、重复次数和晋级策略。"""

    # experiment_id 是报告归档和 GitHub Artifact 名称使用的稳定标识。
    experiment_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    # version 在改变轮数、目标配置或门槛时必须显式递增。
    version: str = Field(min_length=1, max_length=30, pattern=r"^\d+\.\d+\.\d+$")
    # description 说明本实验控制了哪些变量，避免以后误读报告。
    description: str = Field(min_length=1, max_length=500)
    # candidate_profile 必须与目标构造器一致，防止报告把不同依赖组合混为一谈。
    candidate_profile: str = Field(
        default=QWEN_CANDIDATE_PROFILE,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    # 三轮是当前成本与稳定性证据的折中；命令行可在 1 到 10 之间显式覆盖。
    trials: int = Field(default=3, ge=1, le=10)
    # thresholds 与实验版本一起提交，不能运行后再改口径挑选有利结果。
    thresholds: CandidatePromotionThresholds


class AgentCaseStabilityResult(BaseModel):
    """同一黄金样本跨候选轮次的通过稳定性和有限失败规则。"""

    # case_id 与端到端黄金集稳定关联。
    case_id: str
    # tags 支持按 order/security/faq 等场景分析波动。
    tags: list[str]
    # passed_trials 是该样本真正四维全部通过的轮数。
    passed_trials: int = Field(ge=0)
    # total_trials 明确稳定率分母。
    total_trials: int = Field(ge=1)
    # pass_rate 等于 passed_trials / total_trials。
    pass_rate: float = Field(ge=0.0, le=1.0)
    # fully_stable 只有每轮都通过才为 True。
    fully_stable: bool
    # observed_violations 只保留跨轮出现过的稳定规则码，不保存密钥或模型原始响应。
    observed_violations: list[str]


class AgentCandidateExperimentSummary(BaseModel):
    """离线基线、真实候选多轮结果、稳定性和晋级门的完整报告。"""

    # 以下身份字段共同保证两份报告是否可以直接比较。
    experiment_id: str
    experiment_version: str
    dataset_id: str
    dataset_version: str
    # generated_at 使用带时区 UTC 时间，避免本地/CI 时区歧义。
    generated_at: datetime
    # candidate_profile/model 说明实际测试的依赖组合和模型 ID，但不包含 Base URL 或密钥。
    candidate_profile: str
    candidate_model: str
    # candidate_trial_count 是当前报告实际运行轮数，可能由 CLI 覆盖配置文件。
    candidate_trial_count: int = Field(ge=1)
    # planned_chat_calls 是按黄金参考路径估算的调用量；重试或错误路由可能使实际值变化。
    planned_chat_calls_per_trial: int = Field(ge=1)
    planned_total_chat_calls: int = Field(ge=1)
    # baseline_summary 必须是零费用确定性基线，负责先证明代码没有回归。
    baseline_summary: AgentEvaluationSummary
    # candidate_trials 保留每轮逐样本证据，便于定位波动而不是只看平均数。
    candidate_trials: list[AgentEvaluationSummary]
    # mean/worst 防止只汇报最好一次；四个分项帮助判断模型问题发生在哪层。
    mean_overall_pass_rate: float = Field(ge=0.0, le=1.0)
    worst_trial_overall_pass_rate: float = Field(ge=0.0, le=1.0)
    mean_routing_accuracy: float = Field(ge=0.0, le=1.0)
    mean_tool_trajectory_accuracy: float = Field(ge=0.0, le=1.0)
    mean_response_contract_accuracy: float = Field(ge=0.0, le=1.0)
    mean_safety_invariant_accuracy: float = Field(ge=0.0, le=1.0)
    # fully_stable_case_rate 衡量每条场景在所有轮次持续通过的比例。
    fully_stable_cases: int = Field(ge=0)
    fully_stable_case_rate: float = Field(ge=0.0, le=1.0)
    case_stability: list[AgentCaseStabilityResult]
    # promotion_gate 与单轮 100% 数据集门分开，表达当前候选晋级策略。
    promotion_gate_passed: bool
    promotion_gate_failures: list[str]


def load_candidate_experiment_config(path: str) -> AgentCandidateExperimentConfig:
    """从项目相对或绝对 UTF-8 JSON 文件加载候选实验配置。"""

    # 项目路径解析不依赖 PyCharm 的 Working directory。
    config_path = resolve_project_path(path)
    # 明确 UTF-8，保证中文说明在 Windows 和 Linux CI 中一致。
    raw_json = config_path.read_text(encoding="utf-8")
    # TypeAdapter 在任何付费调用前验证全部字段和跨字段门槛关系。
    return TypeAdapter(AgentCandidateExperimentConfig).validate_json(raw_json)


def override_candidate_trial_count(
    config: AgentCandidateExperimentConfig,
    trials: int | None,
) -> AgentCandidateExperimentConfig:
    """用命令行轮数覆盖配置，并重新执行 Pydantic 校验。"""

    # None 表示严格沿用受版本控制的配置，不产生隐藏覆盖。
    if trials is None:
        return config
    # model_dump 后重新 model_validate，确保 1..10 上下界不会被 model_copy 绕过。
    payload = config.model_dump()
    payload["trials"] = trials
    return AgentCandidateExperimentConfig.model_validate(payload)


def estimate_planned_qwen_chat_calls(dataset: AgentEvaluationDataset) -> int:
    """按黄金参考路径估算一轮候选实验的聊天模型请求数。"""

    # 每条样本首先经过一次真实意图分类。
    planned_calls = len(dataset.cases)
    # 后续调用只统计当前候选中启用真实模型的规划和 FAQ 生成。
    for case in dataset.cases:
        if case.expected_intent == Intent.ORDER_STATUS:
            # 每个订单对应一次 call_tool 计划，最后还需要一次 finish/clarify/handoff 计划。
            planned_calls += len(case.expected_tool_names) + 1
        if case.expected_intent == Intent.FAQ:
            # 检索保持 Hash 基线；有证据 FAQ 只增加一次真实 Grounded Generation。
            planned_calls += 1
    # 返回参考路径估算值；SDK 暂时性错误重试可能增加真实服务商请求数。
    return planned_calls


def build_qwen_candidate_evaluation_target(
    settings: Settings | None = None,
) -> tuple[ServiceGraph, ReturnRequestRepository]:
    """构造真实千问聊天决策、确定性检索和完全隔离副作用的候选目标。"""

    # 未显式注入时读取项目根目录 .env，但只允许模型 ID、密钥和 Base URL 影响候选。
    source_settings = settings or Settings()
    # 先导出为 Python 对象，SecretStr 仍保持密文包装，不会被序列化或打印。
    payload = source_settings.model_dump()
    # 固定实验敏感参数，防止个人 .env 调参后仍把报告误认为同一 profile。
    payload.update(
        {
            "environment": "test",
            "telemetry_enabled": False,
            "persistence_backend": "memory",
            "llm_backend": "openai_compatible",
            "llm_temperature": 0.0,
            "llm_timeout_seconds": 30.0,
            "llm_max_retries": 2,
            "intent_confidence_threshold": 0.65,
            "agent_planner_backend": "llm",
            "agent_max_tool_steps": 3,
            # Embedding 保持确定性，实验只比较聊天模型分类、规划和证据回答能力。
            "embedding_backend": "hash",
            "embedding_dimensions": 1024,
            "qdrant_location": ":memory:",
            "qdrant_collection": f"serviceops_qwen_candidate_{uuid4().hex}",
            "knowledge_source_path": "data/seed/knowledge_documents.json",
            "rag_top_k": 3,
            "rag_score_threshold": 0.10,
            # 千问候选1.1.0保持历史Hash原序，不能静默混入第26步重排变量。
            "rag_reranker": "off",
            "rag_chunk_size": 500,
            "rag_chunk_overlap": 80,
            "rag_generation_backend": "llm",
            "rag_max_context_chars": 4000,
        }
    )
    # 重新验证 Settings；缺失 API Key、模型名或 Base URL 会在模型工厂快速失败。
    candidate_settings = Settings.model_validate(payload)

    # 三个工厂分别把同一候选配置绑定到结构化分类、规划和 Grounded Generation。
    classifier_node = build_intent_classifier_node(candidate_settings)
    tool_planner = create_tool_planner(candidate_settings)
    faq_answer_client = create_grounded_answer_client(candidate_settings)
    # 检索器只使用 Hash Embedding 和本轮随机内存 Collection，不产生外部向量费用。
    knowledge_retriever = build_default_knowledge_retriever(candidate_settings)
    # 每轮仓库独立，副作用增量不会跨 trial 累积。
    return_repository = InMemoryReturnRequestRepository(default_order_repository)
    # 图结构与 API 完全相同，只通过显式依赖注入切换评测目标。
    graph = build_service_graph(
        classifier_node=classifier_node,
        order_repository=default_order_repository,
        knowledge_retriever=knowledge_retriever,
        faq_answer_client=faq_answer_client,
        tool_planner=tool_planner,
        return_request_repository=return_repository,
        faq_top_k=candidate_settings.rag_top_k,
        agent_max_tool_steps=candidate_settings.agent_max_tool_steps,
        checkpointer=InMemorySaver(),
    )
    # 调用方必须把这两个对象成对传给单轮评测器，副作用计数才可信。
    return graph, return_repository


def summarize_candidate_experiment(
    *,
    dataset: AgentEvaluationDataset,
    config: AgentCandidateExperimentConfig,
    candidate_model: str,
    baseline_summary: AgentEvaluationSummary,
    candidate_trials: list[AgentEvaluationSummary],
    generated_at: datetime | None = None,
) -> AgentCandidateExperimentSummary:
    """把已经完成的单轮结果聚合为稳定性指标和候选晋级结论。"""

    # 空候选列表无法计算均值和稳定性，必须在报告构造前拒绝。
    if not candidate_trials:
        raise ValueError("候选实验至少需要一轮结果")
    # 实际结果轮数必须与版本化配置一致，避免部分失败轮次被静默丢弃。
    if len(candidate_trials) != config.trials:
        raise ValueError("候选结果轮数必须等于实验配置 trials")
    # 所有单轮都必须使用同一数据集和同一候选 profile 才能聚合。
    for trial in candidate_trials:
        if (
            trial.dataset_id != dataset.dataset_id
            or trial.dataset_version != dataset.version
        ):
            raise ValueError("候选轮次的数据集身份不一致")
        if trial.target_profile != config.candidate_profile:
            raise ValueError("候选轮次的 target_profile 与实验配置不一致")
    # 基线同样必须使用当前数据集，否则无法排除代码/标签版本漂移。
    if (
        baseline_summary.dataset_id != dataset.dataset_id
        or baseline_summary.dataset_version != dataset.version
    ):
        raise ValueError("离线基线的数据集身份不一致")

    # 分别计算均值和最差轮，拒绝只选择最漂亮的一次结果。
    mean_overall = fmean(trial.overall_pass_rate for trial in candidate_trials)
    worst_overall = min(trial.overall_pass_rate for trial in candidate_trials)
    mean_routing = fmean(trial.routing_accuracy for trial in candidate_trials)
    mean_trajectory = fmean(
        trial.tool_trajectory_accuracy for trial in candidate_trials
    )
    mean_contract = fmean(
        trial.response_contract_accuracy for trial in candidate_trials
    )
    mean_safety = fmean(
        trial.safety_invariant_accuracy for trial in candidate_trials
    )

    # 按数据集顺序聚合同一 case，报告和 Git diff 因此保持稳定。
    case_stability: list[AgentCaseStabilityResult] = []
    for case in dataset.cases:
        # 每轮结果转成 case_id 字典，防止未来运行器改变输出顺序后错位比较。
        trial_results = [
            {result.case_id: result for result in trial.results}[case.case_id]
            for trial in candidate_trials
        ]
        # 一条样本只有四个维度全部通过才计入 passed_trials。
        passed_trials = sum(result.passed for result in trial_results)
        # 失败规则做集合去重和排序，避免轮次顺序影响 JSON diff。
        observed_violations = sorted(
            {
                violation
                for result in trial_results
                for violation in result.violations
            }
        )
        case_stability.append(
            AgentCaseStabilityResult(
                case_id=case.case_id,
                tags=case.tags,
                passed_trials=passed_trials,
                total_trials=config.trials,
                pass_rate=passed_trials / config.trials,
                fully_stable=passed_trials == config.trials,
                observed_violations=observed_violations,
            )
        )

    # fully_stable_cases 是所有轮次都通过的场景数，而不是平均通过样本数。
    fully_stable_cases = sum(result.fully_stable for result in case_stability)
    fully_stable_case_rate = fully_stable_cases / len(case_stability)
    # 晋级失败使用稳定码，后续 CI、Dashboard 和面试复盘不依赖中文文案解析。
    promotion_failures: list[str] = []
    thresholds = config.thresholds
    if thresholds.require_baseline_gate_passed and not baseline_summary.quality_gate_passed:
        promotion_failures.append("offline_baseline_gate_failed")
    if mean_overall < thresholds.min_mean_overall_pass_rate:
        promotion_failures.append("mean_overall_pass_rate_below_threshold")
    if worst_overall < thresholds.min_worst_trial_overall_pass_rate:
        promotion_failures.append("worst_trial_overall_pass_rate_below_threshold")
    if fully_stable_case_rate < thresholds.min_fully_stable_case_rate:
        promotion_failures.append("fully_stable_case_rate_below_threshold")
    if mean_safety < thresholds.min_mean_safety_invariant_accuracy:
        promotion_failures.append("mean_safety_invariant_accuracy_below_threshold")

    # 调用量按参考路径估算；实际模型错误路由和 SDK 重试可能增加或减少服务商请求。
    planned_calls_per_trial = estimate_planned_qwen_chat_calls(dataset)
    # 返回强类型总报告，不包含 API Key、Base URL 或完整用户原始会话。
    return AgentCandidateExperimentSummary(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        generated_at=generated_at or datetime.now(UTC),
        candidate_profile=config.candidate_profile,
        candidate_model=candidate_model,
        candidate_trial_count=config.trials,
        planned_chat_calls_per_trial=planned_calls_per_trial,
        planned_total_chat_calls=planned_calls_per_trial * config.trials,
        baseline_summary=baseline_summary,
        candidate_trials=candidate_trials,
        mean_overall_pass_rate=mean_overall,
        worst_trial_overall_pass_rate=worst_overall,
        mean_routing_accuracy=mean_routing,
        mean_tool_trajectory_accuracy=mean_trajectory,
        mean_response_contract_accuracy=mean_contract,
        mean_safety_invariant_accuracy=mean_safety,
        fully_stable_cases=fully_stable_cases,
        fully_stable_case_rate=fully_stable_case_rate,
        case_stability=case_stability,
        promotion_gate_passed=not promotion_failures,
        promotion_gate_failures=promotion_failures,
    )


async def run_candidate_experiment(
    *,
    dataset: AgentEvaluationDataset,
    config: AgentCandidateExperimentConfig,
    candidate_model: str,
    baseline_target_factory: EvaluationTargetFactory = (
        build_offline_agent_evaluation_target
    ),
    candidate_target_factory: EvaluationTargetFactory = (
        build_qwen_candidate_evaluation_target
    ),
) -> AgentCandidateExperimentSummary:
    """先跑一次离线基线，再用全新目标连续运行真实候选并聚合报告。"""

    # 基线每次重建内存索引、Checkpointer 和仓库，证明当前提交仍满足严格 100% 门。
    baseline_graph, baseline_repository = baseline_target_factory()
    baseline_summary = await evaluate_agent_dataset(
        baseline_graph,
        baseline_repository,
        dataset,
        target_profile="offline_deterministic",
    )

    # candidate_trials 按真实执行顺序保存，方便观察第一轮冷启动和后续波动。
    candidate_trials: list[AgentEvaluationSummary] = []
    for _trial_number in range(1, config.trials + 1):
        # 每轮都通过工厂获得新图和新状态边界，不能复用上一轮 Checkpoint/副作用。
        candidate_graph, candidate_repository = candidate_target_factory()
        # 单轮仍使用第十三步的四层硬评估器；自然语言风格不由 LLM 自己打分。
        trial_summary = await evaluate_agent_dataset(
            candidate_graph,
            candidate_repository,
            dataset,
            target_profile=config.candidate_profile,
        )
        # 把完整逐样本证据加入本次实验，不因单轮数据集严格门失败而提前停止。
        candidate_trials.append(trial_summary)

    # 所有轮次完成后统一计算稳定性和版本化晋级策略。
    return summarize_candidate_experiment(
        dataset=dataset,
        config=config,
        candidate_model=candidate_model,
        baseline_summary=baseline_summary,
        candidate_trials=candidate_trials,
    )
