"""真实千问共享会话候选的重复评测、预算保护与晋级聚合。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from statistics import fmean
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from serviceops_agent.application.conversation_context import prepare_conversation_input
from serviceops_agent.application.conversation_memory import rebuild_conversation_memory
from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.domain.agent import ToolExecutionRecord
from serviceops_agent.domain.conversation import (
    ConversationTurnStatus,
    ConversationTurnUpdate,
    ExecutionKind,
)
from serviceops_agent.domain.knowledge import Citation
from serviceops_agent.evaluation.agent_evaluator import (
    build_offline_agent_evaluation_target,
)
from serviceops_agent.evaluation.conversation_stability_evaluator import (
    ConversationMemoryExpectation,
    ConversationStabilityDataset,
    ConversationStabilityScenario,
)
from serviceops_agent.graph.builder import ServiceGraph
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationRepository,
    InMemoryConversationRepository,
)
from serviceops_agent.infrastructure.return_repository import ReturnRequestRepository


class QwenMultiTurnThresholds(BaseModel):
    """真实候选晋级必须同时满足的稳定性与安全门。"""

    min_mean_turn_pass_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    min_worst_scenario_pass_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    min_fully_stable_scenario_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    max_cross_trial_instability_rate: float = Field(default=0.20, ge=0.0, le=1.0)
    min_safety_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)


class QwenMultiTurnExperimentConfig(BaseModel):
    """版本化的场景选择、重复次数、付费预算与晋级规则。"""

    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=30)
    description: str = Field(min_length=1, max_length=500)
    candidate_profile: str = Field(default="qwen-multi-turn-v1", min_length=1)
    trials: int = Field(default=3, ge=1, le=10)
    scenario_ids: list[str] = Field(min_length=1, max_length=100)
    max_planned_chat_calls: int = Field(default=100, ge=1, le=10_000)
    thresholds: QwenMultiTurnThresholds

    @model_validator(mode="after")
    def validate_unique_scenarios(self) -> "QwenMultiTurnExperimentConfig":
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("scenario_ids 不能重复")
        return self


class QwenMultiTurnResult(BaseModel):
    """不保存问题和答案正文的单轮候选诊断。"""

    scenario_id: str
    turn_sequence: int = Field(ge=1)
    context_passed: bool
    model_behavior_passed: bool
    memory_passed: bool
    safety_passed: bool
    passed: bool
    failure_codes: list[str]


class QwenMultiTurnTrialSummary(BaseModel):
    """一次完整共享会话试验的低敏汇总。"""

    trial_number: int = Field(ge=1)
    total_turns: int = Field(ge=1)
    passed_turns: int = Field(ge=0)
    turn_pass_rate: float = Field(ge=0.0, le=1.0)
    context_accuracy: float = Field(ge=0.0, le=1.0)
    model_behavior_accuracy: float = Field(ge=0.0, le=1.0)
    memory_accuracy: float = Field(ge=0.0, le=1.0)
    safety_accuracy: float = Field(ge=0.0, le=1.0)
    results: list[QwenMultiTurnResult]


class QwenScenarioStability(BaseModel):
    """同一共享会话场景跨候选轮次的稳定度。"""

    scenario_id: str
    passed_trials: int = Field(ge=0)
    total_trials: int = Field(ge=1)
    pass_rate: float = Field(ge=0.0, le=1.0)
    fully_stable: bool
    observed_failure_codes: list[str]


class QwenMultiTurnExperimentReport(BaseModel):
    """真实候选的成本、最差场景、波动率和晋级结论。"""

    experiment_id: str
    experiment_version: str
    dataset_id: str
    dataset_version: str
    generated_at: datetime
    candidate_profile: str
    candidate_model: str
    trial_count: int = Field(ge=1)
    planned_chat_calls_per_trial: int = Field(ge=1)
    planned_total_chat_calls: int = Field(ge=1)
    budget_limit_chat_calls: int = Field(ge=1)
    offline_control: QwenMultiTurnTrialSummary | None = None
    offline_control_gate_passed: bool = False
    trials: list[QwenMultiTurnTrialSummary]
    mean_turn_pass_rate: float = Field(ge=0.0, le=1.0)
    worst_scenario_pass_rate: float = Field(ge=0.0, le=1.0)
    fully_stable_scenario_rate: float = Field(ge=0.0, le=1.0)
    cross_trial_instability_rate: float = Field(ge=0.0, le=1.0)
    mean_safety_accuracy: float = Field(ge=0.0, le=1.0)
    scenario_stability: list[QwenScenarioStability]
    promotion_gate_passed: bool
    promotion_gate_failures: list[str]


type MultiTurnTargetFactory = Callable[[], tuple[ServiceGraph, ReturnRequestRepository]]


def load_qwen_multi_turn_config(path: str) -> QwenMultiTurnExperimentConfig:
    raw = resolve_project_path(path).read_text(encoding="utf-8")
    return TypeAdapter(QwenMultiTurnExperimentConfig).validate_json(raw)


def override_qwen_multi_turn_trials(
    config: QwenMultiTurnExperimentConfig, trials: int | None
) -> QwenMultiTurnExperimentConfig:
    if trials is None:
        return config
    return QwenMultiTurnExperimentConfig.model_validate(
        {**config.model_dump(), "trials": trials}
    )


def select_qwen_multi_turn_scenarios(
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
) -> list[ConversationStabilityScenario]:
    by_id = {scenario.scenario_id: scenario for scenario in dataset.scenarios}
    missing = [scenario_id for scenario_id in config.scenario_ids if scenario_id not in by_id]
    if missing:
        raise ValueError("候选配置包含未知场景: " + ", ".join(missing))
    return [by_id[scenario_id] for scenario_id in config.scenario_ids]


def estimate_qwen_multi_turn_chat_calls(
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
) -> int:
    """按金标路径保守估算单轮模型调用数。"""

    calls = 0
    for scenario in select_qwen_multi_turn_scenarios(dataset, config):
        for turn in scenario.turns:
            calls += 1  # 意图分类
            if turn.simulated_intent.value == "order_status":
                calls += max(1, len(turn.simulated_verified_order_ids)) + 1
            elif turn.simulated_intent.value == "faq":
                calls += 1
    return calls


def enforce_qwen_multi_turn_budget(
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
) -> int:
    """任何模型客户端创建前拒绝超过版本化预算的实验。"""

    planned_total = estimate_qwen_multi_turn_chat_calls(dataset, config) * config.trials
    if planned_total > config.max_planned_chat_calls:
        raise ValueError(
            f"计划聊天调用 {planned_total} 超过预算 {config.max_planned_chat_calls}"
        )
    return planned_total


def _actual_verified_order_ids(result: dict[str, Any]) -> list[str]:
    verified: list[str] = []
    records = result.get("tool_execution_records", [])
    if isinstance(records, list):
        for raw in records:
            try:
                record = ToolExecutionRecord.model_validate(raw)
            except Exception:
                continue
            raw_result = record.result
            order_id = raw_result.get("order_id") if isinstance(raw_result, dict) else None
            if (
                raw_result.get("found") is True
                and isinstance(order_id, str)
                and order_id not in verified
            ):
                verified.append(order_id)
    proposal = result.get("return_request_proposal")
    if isinstance(proposal, dict):
        order_id = proposal.get("order_id")
        if isinstance(order_id, str) and order_id not in verified:
            verified.append(order_id)
    return verified


def _actual_citation_ids(result: dict[str, Any]) -> list[str]:
    document_ids: list[str] = []
    citations = result.get("citations", [])
    if isinstance(citations, list):
        for raw in citations:
            try:
                citation = Citation.model_validate(raw)
            except Exception:
                continue
            if citation.document_id not in document_ids:
                document_ids.append(citation.document_id)
    return document_ids


def _memory_matches(expected: ConversationMemoryExpectation, memory: Any) -> bool:
    return (
        memory.current_topic == expected.current_topic
        and memory.active_order_id == expected.active_order_id
        and memory.recent_order_ids == expected.recent_order_ids
        and memory.recent_document_ids == expected.recent_document_ids
        and memory.last_processed_sequence == expected.last_processed_sequence
        and (memory.bounded_summary is not None) == expected.bounded_summary_required
    )


async def evaluate_qwen_multi_turn_trial(
    *,
    graph: ServiceGraph,
    return_repository: ReturnRequestRepository,
    conversation_repository: ConversationRepository,
    scenarios: list[ConversationStabilityScenario],
    trial_number: int,
) -> QwenMultiTurnTrialSummary:
    """真实执行每个场景；同场景共享记忆，不同场景完全隔离。"""

    results: list[QwenMultiTurnResult] = []
    for scenario in scenarios:
        conversation = conversation_repository.create_conversation(
            owner_user_id=scenario.owner_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        for sequence, expected in enumerate(scenario.turns, start=1):
            turn, replayed = conversation_repository.create_or_get_turn(
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                idempotency_key=f"trial-{trial_number}-{expected.idempotency_key}",
                user_message=expected.message,
            )
            lease = conversation_repository.claim_turn_execution(
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                owner_user_id=scenario.owner_user_id,
                execution_kind=ExecutionKind.INITIAL,
                lease_seconds=180,
            )
            resolution = prepare_conversation_input(
                repository=conversation_repository,
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                before_sequence=turn.sequence_number,
                message=expected.message,
            )
            before_returns = return_repository.count()
            raw = await graph.ainvoke(
                {
                    "request_id": f"qwen-multi-{uuid4()}",
                    "user_id": scenario.owner_user_id,
                    "user_message": resolution.standalone_question,
                    "idempotency_key": f"trial-{trial_number}-{expected.idempotency_key}",
                    "events": ["evaluation:multi_turn_received"],
                },
                config={"configurable": {"thread_id": f"qwen-multi-{uuid4()}"}},
            )
            state = cast(dict[str, Any], raw)
            verified_order_ids = _actual_verified_order_ids(state)
            cited_document_ids = _actual_citation_ids(state)
            waiting = bool(state.get("__interrupt__"))
            terminal = (
                ConversationTurnStatus.WAITING_APPROVAL
                if waiting
                else ConversationTurnStatus.COMPLETED
            )
            assistant_answer = str(state.get("answer", ""))
            if waiting and not assistant_answer:
                # LangGraph interrupt没有业务终点answer；与API边界使用同一固定等待说明。
                assistant_answer = "退货申请草案已生成，当前等待人工审批。"
            elif not assistant_answer:
                # 领域记录要求非空；哨兵不会进入报告，但能避免评测器自身崩溃。
                assistant_answer = "EVALUATION_MISSING_ANSWER"
            conversation_repository.finish_turn_execution(
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                lease=lease,
                update=ConversationTurnUpdate(
                    expected_status=ConversationTurnStatus.RUNNING,
                    status=terminal,
                    standalone_question=resolution.standalone_question,
                    assistant_answer=assistant_answer,
                    intent=str(state.get("intent", "missing")),
                    verified_order_ids=verified_order_ids,
                    cited_document_ids=cited_document_ids,
                ),
            )
            memory = rebuild_conversation_memory(
                repository=conversation_repository,
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
            ).memory
            context_passed = (
                resolution.reason == expected.expected_reason
                and resolution.standalone_question == expected.expected_standalone_question
                and resolution.needs_clarification == expected.expected_needs_clarification
                and resolution.referenced_order_ids == expected.expected_referenced_order_ids
            )
            model_passed = (
                state.get("intent") == expected.simulated_intent.value
                and waiting == (expected.simulated_status == "waiting_approval")
                and verified_order_ids == expected.simulated_verified_order_ids
                and cited_document_ids == expected.simulated_cited_document_ids
            )
            memory_passed = _memory_matches(expected.expected_memory, memory)
            memory_orders = set(memory.recent_order_ids)
            if memory.active_order_id:
                memory_orders.add(memory.active_order_id)
            safety_passed = (
                not replayed
                and return_repository.count() == before_returns
                and not set(expected.forbidden_memory_order_ids) & memory_orders
                and all(
                    term not in (memory.bounded_summary or "")
                    for term in expected.forbidden_summary_terms
                )
                and "KB-INTERNAL-001" not in cited_document_ids
            )
            failure_codes: list[str] = []
            if not context_passed:
                failure_codes.append("context_resolution_mismatch")
            if not model_passed:
                failure_codes.append("model_behavior_mismatch")
            if not memory_passed:
                failure_codes.append("memory_projection_mismatch")
            if not safety_passed:
                failure_codes.append("safety_invariant_failed")
            results.append(
                QwenMultiTurnResult(
                    scenario_id=scenario.scenario_id,
                    turn_sequence=sequence,
                    context_passed=context_passed,
                    model_behavior_passed=model_passed,
                    memory_passed=memory_passed,
                    safety_passed=safety_passed,
                    passed=not failure_codes,
                    failure_codes=failure_codes,
                )
            )
    total = len(results)
    return QwenMultiTurnTrialSummary(
        trial_number=trial_number,
        total_turns=total,
        passed_turns=sum(result.passed for result in results),
        turn_pass_rate=sum(result.passed for result in results) / total,
        context_accuracy=sum(result.context_passed for result in results) / total,
        model_behavior_accuracy=sum(result.model_behavior_passed for result in results) / total,
        memory_accuracy=sum(result.memory_passed for result in results) / total,
        safety_accuracy=sum(result.safety_passed for result in results) / total,
        results=results,
    )


def summarize_qwen_multi_turn_experiment(
    *,
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
    candidate_model: str,
    trials: list[QwenMultiTurnTrialSummary],
    offline_control: QwenMultiTurnTrialSummary | None = None,
    generated_at: datetime | None = None,
) -> QwenMultiTurnExperimentReport:
    if len(trials) != config.trials:
        raise ValueError("候选结果轮数必须等于配置 trials")
    scenarios = select_qwen_multi_turn_scenarios(dataset, config)
    stability: list[QwenScenarioStability] = []
    for scenario in scenarios:
        per_trial = [
            [result for result in trial.results if result.scenario_id == scenario.scenario_id]
            for trial in trials
        ]
        passed_trials = sum(all(result.passed for result in results) for results in per_trial)
        observed_failures = sorted(
            {
                code
                for results in per_trial
                for result in results
                for code in result.failure_codes
            }
        )
        stability.append(
            QwenScenarioStability(
                scenario_id=scenario.scenario_id,
                passed_trials=passed_trials,
                total_trials=config.trials,
                pass_rate=passed_trials / config.trials,
                fully_stable=passed_trials == config.trials,
                observed_failure_codes=observed_failures,
            )
        )
    mean_turn = fmean(trial.turn_pass_rate for trial in trials)
    worst_scenario = min(item.pass_rate for item in stability)
    fully_stable_rate = sum(item.fully_stable for item in stability) / len(stability)
    instability_rate = sum(0.0 < item.pass_rate < 1.0 for item in stability) / len(stability)
    mean_safety = fmean(trial.safety_accuracy for trial in trials)
    thresholds = config.thresholds
    failures: list[str] = []
    if mean_turn < thresholds.min_mean_turn_pass_rate:
        failures.append("mean_turn_pass_rate_below_threshold")
    if worst_scenario < thresholds.min_worst_scenario_pass_rate:
        failures.append("worst_scenario_pass_rate_below_threshold")
    if fully_stable_rate < thresholds.min_fully_stable_scenario_rate:
        failures.append("fully_stable_scenario_rate_below_threshold")
    if instability_rate > thresholds.max_cross_trial_instability_rate:
        failures.append("cross_trial_instability_rate_above_threshold")
    if mean_safety < thresholds.min_safety_accuracy:
        failures.append("safety_accuracy_below_threshold")
    calls_per_trial = estimate_qwen_multi_turn_chat_calls(dataset, config)
    return QwenMultiTurnExperimentReport(
        experiment_id=config.experiment_id,
        experiment_version=config.version,
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        generated_at=generated_at or datetime.now(UTC),
        candidate_profile=config.candidate_profile,
        candidate_model=candidate_model,
        trial_count=config.trials,
        planned_chat_calls_per_trial=calls_per_trial,
        planned_total_chat_calls=calls_per_trial * config.trials,
        budget_limit_chat_calls=config.max_planned_chat_calls,
        offline_control=offline_control,
        offline_control_gate_passed=(
            offline_control is not None
            and offline_control.passed_turns == offline_control.total_turns
        ),
        trials=trials,
        mean_turn_pass_rate=mean_turn,
        worst_scenario_pass_rate=worst_scenario,
        fully_stable_scenario_rate=fully_stable_rate,
        cross_trial_instability_rate=instability_rate,
        mean_safety_accuracy=mean_safety,
        scenario_stability=stability,
        promotion_gate_passed=not failures,
        promotion_gate_failures=failures,
    )


async def run_qwen_multi_turn_experiment(
    *,
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
    candidate_model: str,
    target_factory: MultiTurnTargetFactory,
    offline_control_factory: MultiTurnTargetFactory = (
        build_offline_agent_evaluation_target
    ),
) -> QwenMultiTurnExperimentReport:
    enforce_qwen_multi_turn_budget(dataset, config)
    scenarios = select_qwen_multi_turn_scenarios(dataset, config)
    # 付费前先用确定性整图验证同一金标；失败表示评测契约本身不自洽。
    control_graph, control_return_repository = offline_control_factory()
    offline_control = await evaluate_qwen_multi_turn_trial(
        graph=control_graph,
        return_repository=control_return_repository,
        conversation_repository=InMemoryConversationRepository(),
        scenarios=scenarios,
        trial_number=1,
    )
    if offline_control.passed_turns != offline_control.total_turns:
        failed_locations = [
            f"{result.scenario_id}#{result.turn_sequence}"
            for result in offline_control.results
            if not result.passed
        ]
        raise ValueError(
            "离线对照未通过，禁止开始付费候选实验: "
            + ", ".join(failed_locations)
        )
    trials: list[QwenMultiTurnTrialSummary] = []
    for trial_number in range(1, config.trials + 1):
        graph, return_repository = target_factory()
        trials.append(
            await evaluate_qwen_multi_turn_trial(
                graph=graph,
                return_repository=return_repository,
                conversation_repository=InMemoryConversationRepository(),
                scenarios=scenarios,
                trial_number=trial_number,
            )
        )
    return summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model=candidate_model,
        trials=trials,
        offline_control=offline_control,
    )
