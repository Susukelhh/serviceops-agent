"""反馈问题池的幂等、审核和知识候选契约测试。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from serviceops_agent.domain.conversation import ConversationTurnRecord
from serviceops_agent.domain.feedback import (
    FeedbackCategory,
    FeedbackReason,
    FeedbackReview,
    FeedbackSignal,
    FeedbackStatus,
)
from serviceops_agent.infrastructure.feedback_repository import (
    FeedbackConflictError,
    FeedbackRepository,
    InMemoryFeedbackRepository,
    SQLiteFeedbackRepository,
)


@pytest.fixture(params=["memory", "sqlite"])
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> FeedbackRepository:
    if request.param == "memory":
        return InMemoryFeedbackRepository()
    return SQLiteFeedbackRepository(database_path=tmp_path / "feedback.sqlite3")


def _completed_turn() -> ConversationTurnRecord:
    now = datetime.now(UTC)
    return ConversationTurnRecord(
        turn_id=uuid4(),
        conversation_id=uuid4(),
        workflow_thread_id=uuid4(),
        sequence_number=1,
        idempotency_key="turn-feedback-001",
        status="completed",
        user_message="发票税号填错了怎么办？",
        standalone_question="发票税号填错了怎么办？",
        assistant_answer="请提交红冲重开申请。",
        intent="faq",
        cited_document_ids=["KB-INVOICE-001"],
        created_at=now,
        updated_at=now,
    )


def test_feedback_is_idempotent_and_conflicting_reuse_fails(
    repository: FeedbackRepository,
) -> None:
    turn = _completed_turn()
    first, created = repository.record(
        turn=turn,
        owner_user_id="user-001",
        idempotency_key="feedback-key-0001",
        signal=FeedbackSignal.UNHELPFUL,
        reason=FeedbackReason.MISSING_INFORMATION,
    )
    replay, replay_created = repository.record(
        turn=turn,
        owner_user_id="user-001",
        idempotency_key="feedback-key-0001",
        signal=FeedbackSignal.UNHELPFUL,
        reason=FeedbackReason.MISSING_INFORMATION,
    )

    assert created is True
    assert replay_created is False
    assert replay == first
    assert repository.list_open() == [first]

    with pytest.raises(FeedbackConflictError):
        repository.record(
            turn=turn,
            owner_user_id="user-001",
            idempotency_key="feedback-key-0001",
            signal=FeedbackSignal.HELPFUL,
            reason=None,
        )


def test_human_review_creates_versionable_knowledge_candidate(
    repository: FeedbackRepository,
) -> None:
    feedback, _ = repository.record(
        turn=_completed_turn(),
        owner_user_id="user-001",
        idempotency_key="feedback-key-0002",
        signal=FeedbackSignal.UNHELPFUL,
        reason=FeedbackReason.MISSING_INFORMATION,
    )
    decision = FeedbackReview(
        category=FeedbackCategory.KNOWLEDGE_GAP,
        proposed_title="电子发票红冲申请材料",
        proposed_answer="提交红冲重开申请时，需要提供原发票号码和正确的企业抬头信息。",
    )

    reviewed = repository.review(
        feedback_id=feedback.feedback_id,
        reviewer_id="curator-001",
        decision=decision,
    )
    replay = repository.review(
        feedback_id=feedback.feedback_id,
        reviewer_id="curator-001",
        decision=decision,
    )
    candidates = repository.list_knowledge_candidates()

    assert reviewed.status == FeedbackStatus.KNOWLEDGE_CANDIDATE
    assert replay == reviewed
    assert repository.list_open() == []
    assert len(candidates) == 1
    assert candidates[0].source_feedback_id == feedback.feedback_id
    assert candidates[0].title == "电子发票红冲申请材料"

    with pytest.raises(FeedbackConflictError):
        repository.review(
            feedback_id=feedback.feedback_id,
            reviewer_id="curator-002",
            decision=FeedbackReview(category=FeedbackCategory.NOT_ACTIONABLE),
        )

    assert repository.delete_for_conversation(
        conversation_id=feedback.conversation_id
    ) == 1
    assert repository.list_knowledge_candidates() == []
