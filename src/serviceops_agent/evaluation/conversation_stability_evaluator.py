"""完全离线的多轮指代、结构化记忆、幂等与隔离质量门。"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from serviceops_agent.application.conversation_context import prepare_conversation_input
from serviceops_agent.application.conversation_memory import rebuild_conversation_memory
from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.domain.conversation import (
    ConversationTurnStatus,
    ConversationTurnUpdate,
    ExecutionKind,
    ExecutionLeaseState,
    FollowUpResolutionReason,
)
from serviceops_agent.domain.enums import Intent
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationRepository,
    InMemoryConversationRepository,
)


class ConversationStabilityThresholds(BaseModel):
    """与数据集一起版本化的四项确定性发布门。"""

    min_overall_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    min_resolution_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    min_memory_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    min_execution_safety_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    min_isolation_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)


class ConversationMemoryExpectation(BaseModel):
    """一轮结束后必须精确匹配的有限记忆投影。"""

    current_topic: str | None
    active_order_id: str | None
    recent_order_ids: list[str] = Field(default_factory=list, max_length=10)
    recent_document_ids: list[str] = Field(default_factory=list, max_length=10)
    last_processed_sequence: int = Field(ge=0)
    bounded_summary_required: bool = False


class ConversationStabilityTurn(BaseModel):
    """一轮输入、确定性解析金标和模拟的可信图终态。"""

    message: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    expected_reason: FollowUpResolutionReason
    expected_standalone_question: str = Field(min_length=1, max_length=4000)
    expected_needs_clarification: bool = False
    expected_referenced_order_ids: list[str] = Field(default_factory=list, max_length=10)
    simulated_status: Literal["completed", "waiting_approval"] = "completed"
    simulated_intent: Intent
    simulated_verified_order_ids: list[str] = Field(default_factory=list, max_length=10)
    simulated_cited_document_ids: list[str] = Field(default_factory=list, max_length=10)
    expected_memory: ConversationMemoryExpectation
    forbidden_memory_order_ids: list[str] = Field(default_factory=list, max_length=10)
    forbidden_summary_terms: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_turn_contract(self) -> "ConversationStabilityTurn":
        """拒绝会让安全结论自相矛盾的金标。"""

        if set(self.expected_referenced_order_ids) & set(
            self.forbidden_memory_order_ids
        ):
            raise ValueError("同一订单不能同时期望引用和禁止进入记忆")
        if (
            self.simulated_status == "waiting_approval"
            and self.simulated_intent != Intent.RETURN_REQUEST
        ):
            raise ValueError("waiting_approval 只能模拟 return_request")
        return self


class ConversationStabilityScenario(BaseModel):
    """共享同一会话记忆的有序多轮场景。"""

    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=80)
    owner_user_id: str = Field(min_length=1, max_length=64)
    turns: list[ConversationStabilityTurn] = Field(min_length=2, max_length=50)


class ConversationStabilityDataset(BaseModel):
    """版本化多轮稳定性金标与发布阈值。"""

    dataset_id: str = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=30)
    description: str = Field(min_length=1, max_length=500)
    thresholds: ConversationStabilityThresholds
    scenarios: list[ConversationStabilityScenario] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_ids_and_keys(self) -> "ConversationStabilityDataset":
        """场景ID全局唯一，幂等键在各场景内也必须唯一。"""

        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("多轮评测 scenario_id 不能重复")
        for scenario in self.scenarios:
            keys = [turn.idempotency_key for turn in scenario.turns]
            if len(keys) != len(set(keys)):
                raise ValueError("同一场景的 idempotency_key 不能重复")
        return self


class ConversationStabilityTurnResult(BaseModel):
    """不含问题、答案和订单正文的单轮诊断。"""

    scenario_id: str
    turn_sequence: int = Field(ge=1)
    resolution_passed: bool
    memory_passed: bool
    execution_safety_passed: bool
    isolation_passed: bool
    passed: bool
    actual_reason: FollowUpResolutionReason
    memory_version: int = Field(ge=0)
    failure_codes: list[str]


class ConversationStabilitySummary(BaseModel):
    """CI使用的四维指标、聚合结论和低敏逐轮证据。"""

    dataset_id: str
    dataset_version: str
    target_profile: Literal["offline-conversation-state-v1"]
    total_turns: int = Field(ge=1)
    passed_turns: int = Field(ge=0)
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    resolution_accuracy: float = Field(ge=0.0, le=1.0)
    memory_accuracy: float = Field(ge=0.0, le=1.0)
    execution_safety_accuracy: float = Field(ge=0.0, le=1.0)
    isolation_accuracy: float = Field(ge=0.0, le=1.0)
    quality_gate_passed: bool
    quality_gate_failures: list[str]
    results: list[ConversationStabilityTurnResult]


def load_conversation_stability_dataset(path: str) -> ConversationStabilityDataset:
    """读取UTF-8 JSON，并在执行任何状态转换前完成强类型校验。"""

    raw = resolve_project_path(path).read_text(encoding="utf-8")
    return TypeAdapter(ConversationStabilityDataset).validate_json(raw)


def _memory_matches(
    expectation: ConversationMemoryExpectation,
    *,
    current_topic: str | None,
    active_order_id: str | None,
    recent_order_ids: list[str],
    recent_document_ids: list[str],
    last_processed_sequence: int,
    bounded_summary: str | None,
) -> bool:
    """精确比较允许进入报告判定的记忆字段。"""

    return (
        current_topic == expectation.current_topic
        and active_order_id == expectation.active_order_id
        and recent_order_ids == expectation.recent_order_ids
        and recent_document_ids == expectation.recent_document_ids
        and last_processed_sequence == expectation.last_processed_sequence
        and (bounded_summary is not None) == expectation.bounded_summary_required
    )


def _quality_gate_failures(
    dataset: ConversationStabilityDataset,
    *,
    overall: float,
    resolution: float,
    memory: float,
    execution: float,
    isolation: float,
) -> list[str]:
    """把聚合阈值失败转换为稳定机器码。"""

    thresholds = dataset.thresholds
    failures: list[str] = []
    if overall < thresholds.min_overall_pass_rate:
        failures.append("overall_pass_rate_below_threshold")
    if resolution < thresholds.min_resolution_accuracy:
        failures.append("resolution_accuracy_below_threshold")
    if memory < thresholds.min_memory_accuracy:
        failures.append("memory_accuracy_below_threshold")
    if execution < thresholds.min_execution_safety_accuracy:
        failures.append("execution_safety_accuracy_below_threshold")
    if isolation < thresholds.min_isolation_accuracy:
        failures.append("isolation_accuracy_below_threshold")
    return failures


def evaluate_conversation_stability(
    dataset: ConversationStabilityDataset,
    *,
    repository: ConversationRepository | None = None,
) -> ConversationStabilitySummary:
    """运行多轮状态控制层；不调用模型、向量库或外部网络。"""

    target_repository = (
        repository if repository is not None else InMemoryConversationRepository()
    )
    results: list[ConversationStabilityTurnResult] = []
    for scenario in dataset.scenarios:
        conversation = target_repository.create_conversation(
            owner_user_id=scenario.owner_user_id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        previous_answer_sentinels: list[str] = []
        for sequence, expected in enumerate(scenario.turns, start=1):
            turn, replayed = target_repository.create_or_get_turn(
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                idempotency_key=expected.idempotency_key,
                user_message=expected.message,
            )
            lease = target_repository.claim_turn_execution(
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                owner_user_id=scenario.owner_user_id,
                execution_kind=ExecutionKind.INITIAL,
                lease_seconds=90,
            )
            resolution = prepare_conversation_input(
                repository=target_repository,
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                before_sequence=turn.sequence_number,
                message=expected.message,
            )
            resolution_passed = (
                resolution.reason == expected.expected_reason
                and resolution.standalone_question
                == expected.expected_standalone_question
                and resolution.needs_clarification
                == expected.expected_needs_clarification
                and resolution.referenced_order_ids
                == expected.expected_referenced_order_ids
            )

            answer_sentinel = f"OFFLINE_ASSISTANT_SENTINEL_{scenario.scenario_id}_{sequence}"
            terminal_status = (
                ConversationTurnStatus.COMPLETED
                if expected.simulated_status == "completed"
                else ConversationTurnStatus.WAITING_APPROVAL
            )
            completed = target_repository.finish_turn_execution(
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                lease=lease,
                update=ConversationTurnUpdate(
                    expected_status=ConversationTurnStatus.RUNNING,
                    status=terminal_status,
                    standalone_question=resolution.standalone_question,
                    assistant_answer=answer_sentinel,
                    intent=expected.simulated_intent.value,
                    verified_order_ids=expected.simulated_verified_order_ids,
                    cited_document_ids=expected.simulated_cited_document_ids,
                ),
            )
            rebuilt = rebuild_conversation_memory(
                repository=target_repository,
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
            )
            rebuilt_again = rebuild_conversation_memory(
                repository=target_repository,
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
            )
            stored_lease = target_repository.get_turn_execution_lease(
                turn_id=turn.turn_id
            )
            replay_turn, replayed_after_finish = target_repository.create_or_get_turn(
                conversation_id=conversation.conversation_id,
                owner_user_id=scenario.owner_user_id,
                idempotency_key=expected.idempotency_key,
                user_message=expected.message,
            )

            memory = rebuilt.memory
            memory_passed = _memory_matches(
                expected.expected_memory,
                current_topic=memory.current_topic,
                active_order_id=memory.active_order_id,
                recent_order_ids=memory.recent_order_ids,
                recent_document_ids=memory.recent_document_ids,
                last_processed_sequence=memory.last_processed_sequence,
                bounded_summary=memory.bounded_summary,
            )
            execution_safety_passed = (
                not replayed
                and replayed_after_finish
                and replay_turn.turn_id == turn.turn_id
                and completed.status == terminal_status
                and stored_lease is not None
                and stored_lease.state == ExecutionLeaseState.RELEASED
                and stored_lease.fence_generation == 1
                and rebuilt_again.memory == memory
            )
            memory_order_ids = set(memory.recent_order_ids)
            if memory.active_order_id is not None:
                memory_order_ids.add(memory.active_order_id)
            summary = memory.bounded_summary or ""
            isolation_passed = (
                not set(expected.forbidden_memory_order_ids)
                & (
                    memory_order_ids
                    | set(resolution.referenced_order_ids)
                )
                and all(term not in summary for term in expected.forbidden_summary_terms)
                and all(
                    sentinel not in resolution.standalone_question
                    for sentinel in previous_answer_sentinels
                )
            )
            failure_codes: list[str] = []
            if not resolution_passed:
                failure_codes.append("resolution_mismatch")
            if not memory_passed:
                failure_codes.append("memory_projection_mismatch")
            if not execution_safety_passed:
                failure_codes.append("execution_or_replay_invariant_failed")
            if not isolation_passed:
                failure_codes.append("context_or_summary_isolation_failed")
            results.append(
                ConversationStabilityTurnResult(
                    scenario_id=scenario.scenario_id,
                    turn_sequence=sequence,
                    resolution_passed=resolution_passed,
                    memory_passed=memory_passed,
                    execution_safety_passed=execution_safety_passed,
                    isolation_passed=isolation_passed,
                    passed=not failure_codes,
                    actual_reason=resolution.reason,
                    memory_version=memory.memory_version,
                    failure_codes=failure_codes,
                )
            )
            previous_answer_sentinels.append(answer_sentinel)

    total = len(results)
    overall = sum(result.passed for result in results) / total
    resolution_accuracy = sum(result.resolution_passed for result in results) / total
    memory_accuracy = sum(result.memory_passed for result in results) / total
    execution_accuracy = (
        sum(result.execution_safety_passed for result in results) / total
    )
    isolation_accuracy = sum(result.isolation_passed for result in results) / total
    gate_failures = _quality_gate_failures(
        dataset,
        overall=overall,
        resolution=resolution_accuracy,
        memory=memory_accuracy,
        execution=execution_accuracy,
        isolation=isolation_accuracy,
    )
    return ConversationStabilitySummary(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        target_profile="offline-conversation-state-v1",
        total_turns=total,
        passed_turns=sum(result.passed for result in results),
        overall_pass_rate=overall,
        resolution_accuracy=resolution_accuracy,
        memory_accuracy=memory_accuracy,
        execution_safety_accuracy=execution_accuracy,
        isolation_accuracy=isolation_accuracy,
        quality_gate_passed=not gate_failures,
        quality_gate_failures=gate_failures,
        results=results,
    )
