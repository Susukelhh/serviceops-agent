"""多轮线上影子观察、低敏窗口聚合与告警/回滚决策。"""

import hashlib
from collections import Counter
from enum import StrEnum
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.domain.conversation import (
    ConversationMemory,
    ConversationTurnRecord,
    ConversationTurnStatus,
    FollowUpResolutionReason,
)


class ShadowOutcome(StrEnum):
    """影子指标允许使用的有限业务终态。"""

    COMPLETED = "completed"
    APPROVAL_REQUIRED = "approval_required"
    CLARIFICATION = "clarification"
    HUMAN_HANDOFF = "human_handoff"
    FAILED = "failed"


class ShadowObservation(BaseModel):
    """单轮低敏投影；Schema没有问题、答案、用户或业务标识字段。"""

    intent: Literal["faq", "order_status", "return_request", "human_handoff", "unknown"]
    outcome: ShadowOutcome
    resolution_reason: FollowUpResolutionReason
    model_failure: bool
    evidence_abstention: bool
    ambiguous_context: bool
    safety_violation_codes: list[str]


class ShadowWindowSnapshot(BaseModel):
    """一个告警窗口内的有限计数和比例。"""

    candidate_id: str = Field(
        default="local-baseline",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9.-]*$",
    )
    total_observations: int = Field(ge=0)
    model_failures: int = Field(ge=0)
    evidence_abstentions: int = Field(ge=0)
    ambiguous_contexts: int = Field(ge=0)
    human_handoffs: int = Field(ge=0)
    safety_violations: int = Field(ge=0)
    model_failure_rate: float = Field(ge=0.0, le=1.0)
    evidence_abstention_rate: float = Field(ge=0.0, le=1.0)
    ambiguous_context_rate: float = Field(ge=0.0, le=1.0)
    human_handoff_rate: float = Field(ge=0.0, le=1.0)
    safety_violation_rate: float = Field(ge=0.0, le=1.0)
    safety_violation_code_counts: dict[str, int]


class ShadowAlertPolicy(BaseModel):
    """版本化的最小窗口、漂移阈值和零容忍安全策略。"""

    policy_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=30)
    min_window_observations: int = Field(default=100, ge=10, le=1_000_000)
    max_model_failure_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    max_evidence_abstention_rate: float = Field(default=0.30, ge=0.0, le=1.0)
    max_ambiguous_context_rate: float = Field(default=0.35, ge=0.0, le=1.0)
    max_human_handoff_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_safety_violation_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class ShadowReleaseDecision(BaseModel):
    """聚合窗口对候选流量给出的有限处置建议。"""

    action: Literal["observe", "continue", "investigate", "rollback"]
    sufficient_sample: bool
    reason_codes: list[str]
    snapshot: ShadowWindowSnapshot


SAFE_SHADOW_INTENTS = frozenset(
    {"faq", "order_status", "return_request", "human_handoff"}
)


def load_shadow_alert_policy(path: str) -> ShadowAlertPolicy:
    raw = resolve_project_path(path).read_text(encoding="utf-8")
    return TypeAdapter(ShadowAlertPolicy).validate_json(raw)


def should_sample_shadow_observation(request_id: str, sample_rate: float) -> bool:
    """稳定哈希采样；相同请求重试不会因随机数产生不同选择。"""

    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError("影子采样率必须位于0到1")
    if sample_rate == 0.0:
        return False
    if sample_rate == 1.0:
        return True
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], byteorder="big") / 2**64
    return bucket < sample_rate


def _shadow_outcome(result: dict[str, Any], turn: ConversationTurnRecord) -> ShadowOutcome:
    if turn.status == ConversationTurnStatus.FAILED:
        return ShadowOutcome.FAILED
    if turn.status == ConversationTurnStatus.WAITING_APPROVAL:
        return ShadowOutcome.APPROVAL_REQUIRED
    if result.get("requires_human") is True:
        return ShadowOutcome.HUMAN_HANDOFF
    if result.get("needs_clarification") is True:
        return ShadowOutcome.CLARIFICATION
    return ShadowOutcome.COMPLETED


def build_shadow_observation(
    *,
    request_id: str,
    result: dict[str, Any],
    resolution_reason: FollowUpResolutionReason,
    turn: ConversationTurnRecord,
    memory: ConversationMemory | None,
    enabled: bool,
    sample_rate: float,
) -> ShadowObservation | None:
    """从已完成终态构造低敏观察，不额外调用模型或读取历史正文。"""

    if not enabled or not should_sample_shadow_observation(request_id, sample_rate):
        return None
    raw_intent = str(result.get("intent", "unknown"))
    intent = raw_intent if raw_intent in SAFE_SHADOW_INTENTS else "unknown"
    outcome = _shadow_outcome(result, turn)
    model_failure = bool(result.get("llm_failure_code"))
    evidence_abstention = (
        intent == "faq"
        and result.get("faq_answer_grounded") is not True
        and outcome in {ShadowOutcome.HUMAN_HANDOFF, ShadowOutcome.CLARIFICATION}
    )
    violations: list[str] = []
    if (
        intent == "faq"
        and outcome == ShadowOutcome.COMPLETED
        and result.get("faq_answer_grounded") is not True
    ):
        violations.append("ungrounded_faq_auto_answer")
    if (
        outcome == ShadowOutcome.APPROVAL_REQUIRED
        and result.get("return_request_id") is not None
    ):
        violations.append("approval_pending_contains_write_result")
    if model_failure and outcome != ShadowOutcome.HUMAN_HANDOFF:
        violations.append("model_failure_without_handoff")
    if memory is not None:
        if (
            memory.active_order_id is not None
            and memory.active_order_id not in memory.recent_order_ids
        ):
            violations.append("active_order_missing_from_recent_orders")
        if (
            memory.active_order_id is not None
            and intent in {"faq", "human_handoff"}
        ):
            violations.append("cross_topic_active_order_retained")
    return ShadowObservation(
        intent=intent,  # type: ignore[arg-type]
        outcome=outcome,
        resolution_reason=resolution_reason,
        model_failure=model_failure,
        evidence_abstention=evidence_abstention,
        ambiguous_context=(
            resolution_reason == FollowUpResolutionReason.AMBIGUOUS_ORDER_REFERENCE
        ),
        safety_violation_codes=sorted(set(violations)),
    )


class InMemoryShadowWindow:
    """单进程测试/开发窗口；生产聚合应以OTel后端为准。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._observations: list[ShadowObservation] = []

    def add(self, observation: ShadowObservation) -> None:
        with self._lock:
            self._observations.append(observation)

    def snapshot(self) -> ShadowWindowSnapshot:
        with self._lock:
            observations = list(self._observations)
        return summarize_shadow_observations(observations)

    def clear(self) -> None:
        with self._lock:
            self._observations.clear()


def summarize_shadow_observations(
    observations: list[ShadowObservation],
    *,
    candidate_id: str = "local-baseline",
) -> ShadowWindowSnapshot:
    total = len(observations)
    model_failures = sum(item.model_failure for item in observations)
    evidence_abstentions = sum(item.evidence_abstention for item in observations)
    ambiguous_contexts = sum(item.ambiguous_context for item in observations)
    human_handoffs = sum(
        item.outcome == ShadowOutcome.HUMAN_HANDOFF for item in observations
    )
    safety_counts = Counter(
        code for item in observations for code in item.safety_violation_codes
    )
    safety_violations = sum(bool(item.safety_violation_codes) for item in observations)

    def rate(count: int) -> float:
        return count / total if total else 0.0

    return ShadowWindowSnapshot(
        candidate_id=candidate_id,
        total_observations=total,
        model_failures=model_failures,
        evidence_abstentions=evidence_abstentions,
        ambiguous_contexts=ambiguous_contexts,
        human_handoffs=human_handoffs,
        safety_violations=safety_violations,
        model_failure_rate=rate(model_failures),
        evidence_abstention_rate=rate(evidence_abstentions),
        ambiguous_context_rate=rate(ambiguous_contexts),
        human_handoff_rate=rate(human_handoffs),
        safety_violation_rate=rate(safety_violations),
        safety_violation_code_counts=dict(sorted(safety_counts.items())),
    )


def evaluate_shadow_release(
    snapshot: ShadowWindowSnapshot,
    policy: ShadowAlertPolicy,
) -> ShadowReleaseDecision:
    """安全红线即时回滚；体验代理指标达到样本量后才触发调查。"""

    if snapshot.safety_violation_rate > policy.max_safety_violation_rate:
        return ShadowReleaseDecision(
            action="rollback",
            sufficient_sample=(
                snapshot.total_observations >= policy.min_window_observations
            ),
            reason_codes=["safety_violation_rate_above_threshold"],
            snapshot=snapshot,
        )
    sufficient = snapshot.total_observations >= policy.min_window_observations
    if not sufficient:
        return ShadowReleaseDecision(
            action="observe",
            sufficient_sample=False,
            reason_codes=["minimum_window_not_reached"],
            snapshot=snapshot,
        )
    rollback_reasons: list[str] = []
    if snapshot.model_failure_rate > policy.max_model_failure_rate:
        rollback_reasons.append("model_failure_rate_above_threshold")
    if rollback_reasons:
        return ShadowReleaseDecision(
            action="rollback",
            sufficient_sample=True,
            reason_codes=rollback_reasons,
            snapshot=snapshot,
        )
    investigation_reasons: list[str] = []
    if snapshot.evidence_abstention_rate > policy.max_evidence_abstention_rate:
        investigation_reasons.append("evidence_abstention_rate_above_threshold")
    if snapshot.ambiguous_context_rate > policy.max_ambiguous_context_rate:
        investigation_reasons.append("ambiguous_context_rate_above_threshold")
    if snapshot.human_handoff_rate > policy.max_human_handoff_rate:
        investigation_reasons.append("human_handoff_rate_above_threshold")
    return ShadowReleaseDecision(
        action="investigate" if investigation_reasons else "continue",
        sufficient_sample=True,
        reason_codes=investigation_reasons,
        snapshot=snapshot,
    )
