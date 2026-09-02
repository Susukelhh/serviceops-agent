"""第54步多轮影子采样、低敏投影、窗口和回滚策略测试。"""

from datetime import UTC, datetime
from uuid import uuid4

from serviceops_agent.application.conversation_shadow import (
    ShadowObservation,
    ShadowOutcome,
    build_shadow_observation,
    evaluate_shadow_release,
    load_shadow_alert_policy,
    should_sample_shadow_observation,
    summarize_shadow_observations,
)
from serviceops_agent.domain.conversation import (
    ConversationMemory,
    ConversationTurnRecord,
    ConversationTurnStatus,
    FollowUpResolutionReason,
)


def _turn(status: ConversationTurnStatus = ConversationTurnStatus.COMPLETED):
    now = datetime.now(UTC)
    return ConversationTurnRecord(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        workflow_thread_id=uuid4(),
        sequence_number=1,
        idempotency_key="shadow-test-0001",
        status=status,
        user_message="sensitive-user-message",
        standalone_question="sensitive-standalone-question",
        assistant_answer="sensitive-model-answer",
        intent="faq",
        created_at=now,
        updated_at=now,
    )


def _observation(
    *,
    outcome: ShadowOutcome = ShadowOutcome.COMPLETED,
    model_failure: bool = False,
    evidence_abstention: bool = False,
    ambiguous_context: bool = False,
    safety_codes: list[str] | None = None,
) -> ShadowObservation:
    return ShadowObservation(
        intent="faq",
        outcome=outcome,
        resolution_reason=FollowUpResolutionReason.INDEPENDENT_QUESTION,
        model_failure=model_failure,
        evidence_abstention=evidence_abstention,
        ambiguous_context=ambiguous_context,
        safety_violation_codes=safety_codes or [],
    )


def test_shadow_sampling_is_deterministic_and_honors_boundaries() -> None:
    assert should_sample_shadow_observation("request-001", 0.0) is False
    assert should_sample_shadow_observation("request-001", 1.0) is True
    first = should_sample_shadow_observation("request-stable", 0.37)
    assert should_sample_shadow_observation("request-stable", 0.37) is first


def test_shadow_projection_detects_safety_without_serializing_content() -> None:
    observation = build_shadow_observation(
        request_id="request-001",
        result={
            "intent": "faq",
            "requires_human": False,
            "needs_clarification": False,
            "faq_answer_grounded": False,
            "answer": "sensitive-model-answer",
        },
        resolution_reason=FollowUpResolutionReason.INDEPENDENT_QUESTION,
        turn=_turn(),
        memory=ConversationMemory(),
        enabled=True,
        sample_rate=1.0,
    )

    assert observation is not None
    assert observation.safety_violation_codes == ["ungrounded_faq_auto_answer"]
    serialized = observation.model_dump_json()
    assert "sensitive-user-message" not in serialized
    assert "sensitive-standalone-question" not in serialized
    assert "sensitive-model-answer" not in serialized
    assert "request-001" not in serialized


def test_approval_write_and_cross_topic_focus_are_safety_violations() -> None:
    observation = build_shadow_observation(
        request_id="request-approval",
        result={
            "intent": "faq",
            "return_request_id": "RET-SHOULD-NOT-LEAK",
            "__interrupt__": [object()],
        },
        resolution_reason=FollowUpResolutionReason.INDEPENDENT_QUESTION,
        turn=_turn(ConversationTurnStatus.WAITING_APPROVAL),
        memory=ConversationMemory(
            active_order_id="SO100001",
            recent_order_ids=["SO100001"],
        ),
        enabled=True,
        sample_rate=1.0,
    )

    assert observation is not None
    assert observation.safety_violation_codes == [
        "approval_pending_contains_write_result",
        "cross_topic_active_order_retained",
    ]


def test_shadow_window_uses_sample_floor_but_safety_rolls_back_immediately() -> None:
    policy = load_shadow_alert_policy(
        "data/evaluation/conversation_shadow_alert_policy.json"
    )
    small_safe = summarize_shadow_observations([_observation()] * 10)
    assert evaluate_shadow_release(small_safe, policy).action == "observe"

    one_unsafe = summarize_shadow_observations(
        [_observation(safety_codes=["ungrounded_faq_auto_answer"])]
    )
    unsafe_decision = evaluate_shadow_release(one_unsafe, policy)
    assert unsafe_decision.action == "rollback"
    assert unsafe_decision.sufficient_sample is False


def test_model_failure_rolls_back_and_business_drift_only_investigates() -> None:
    policy = load_shadow_alert_policy(
        "data/evaluation/conversation_shadow_alert_policy.json"
    )
    model_window = summarize_shadow_observations(
        [_observation(model_failure=index < 6) for index in range(100)]
    )
    assert evaluate_shadow_release(model_window, policy).action == "rollback"

    handoff_window = summarize_shadow_observations(
        [
            _observation(
                outcome=(
                    ShadowOutcome.HUMAN_HANDOFF
                    if index < 41
                    else ShadowOutcome.COMPLETED
                )
            )
            for index in range(100)
        ]
    )
    decision = evaluate_shadow_release(handoff_window, policy)
    assert decision.action == "investigate"
    assert decision.reason_codes == ["human_handoff_rate_above_threshold"]


def test_healthy_full_window_continues() -> None:
    policy = load_shadow_alert_policy(
        "data/evaluation/conversation_shadow_alert_policy.json"
    )
    snapshot = summarize_shadow_observations([_observation()] * 100)
    decision = evaluate_shadow_release(snapshot, policy)
    assert decision.action == "continue"
    assert decision.reason_codes == []
