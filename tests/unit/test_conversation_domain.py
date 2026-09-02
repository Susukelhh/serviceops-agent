"""多轮会话领域契约的纯内存测试。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from serviceops_agent.domain.conversation import (
    ConversationContext,
    ConversationMemory,
    ConversationRecord,
    ConversationStatus,
    ConversationTurnRecord,
    RecentConversationTurn,
)


def test_conversation_memory_normalizes_and_deduplicates_order_ids() -> None:
    """会话槽位应保存规范订单号，避免同一订单产生多个记忆键。"""

    memory = ConversationMemory(
        active_order_id=" so100001 ",
        recent_order_ids=["so100001", "SO100002", "SO100001"],
    )

    assert memory.active_order_id == "SO100001"
    assert memory.recent_order_ids == ["SO100001", "SO100002"]


def test_conversation_memory_rejects_untrusted_order_identifier() -> None:
    """自由文本不能伪装成可跨轮复用的订单标识。"""

    with pytest.raises(ValidationError, match="recent_order_ids"):
        ConversationMemory(recent_order_ids=["上一单"])


def test_conversation_rejects_unbounded_document_identifier() -> None:
    """异常引用ID必须在写入轮次和记忆前被拒绝，不能在同步阶段才触发500。"""

    with pytest.raises(ValidationError, match="recent_document_ids"):
        ConversationMemory(recent_document_ids=["X" * 101])

    timestamp = datetime.now(UTC)
    with pytest.raises(ValidationError, match="cited_document_ids"):
        ConversationTurnRecord(
            turn_id=uuid4(),
            conversation_id=uuid4(),
            workflow_thread_id=uuid4(),
            sequence_number=1,
            idempotency_key="document-bound-0001",
            user_message="测试引用",
            cited_document_ids=["X" * 101],
            created_at=timestamp,
            updated_at=timestamp,
        )


def test_conversation_record_requires_timezone_aware_timeline() -> None:
    """跨进程持久化记录必须拥有无歧义时间和有效过期边界。"""

    created_at = datetime.now(UTC)
    record = ConversationRecord(
        conversation_id=uuid4(),
        owner_user_id="user-001",
        status=ConversationStatus.ACTIVE,
        created_at=created_at,
        updated_at=created_at,
        expires_at=created_at + timedelta(days=7),
    )

    assert record.expires_at > record.created_at

    with pytest.raises(ValidationError, match="必须包含时区"):
        ConversationRecord(
            conversation_id=uuid4(),
            owner_user_id="user-001",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            expires_at=datetime.now() + timedelta(days=7),
        )


def test_turn_record_keeps_conversation_and_workflow_identifiers_separate() -> None:
    """一个会话轮次必须显式映射到独立的 LangGraph 工作流线程。"""

    timestamp = datetime.now(UTC)
    conversation_id = uuid4()
    workflow_thread_id = uuid4()
    turn = ConversationTurnRecord(
        turn_id=uuid4(),
        conversation_id=conversation_id,
        workflow_thread_id=workflow_thread_id,
        sequence_number=2,
        idempotency_key="conversation-turn-002",
        user_message="那运费谁承担？",
        created_at=timestamp,
        updated_at=timestamp,
    )

    assert turn.conversation_id == conversation_id
    assert turn.workflow_thread_id == workflow_thread_id
    assert turn.conversation_id != turn.workflow_thread_id


def test_model_context_is_bounded_and_excludes_identity_and_workflow_fields() -> None:
    """追问解析器只能看到有限历史，不能接收用户身份、审批线程或幂等键。"""

    context = ConversationContext(
        conversation_id=uuid4(),
        memory=ConversationMemory(
            current_topic="return_policy",
            active_order_id="SO100001",
        ),
        recent_turns=[
            RecentConversationTurn(
                sequence_number=1,
                user_message="SO100001符合七天无理由吗？",
                intent="faq",
                verified_order_ids=["SO100001"],
                cited_document_ids=["KB-RETURN-001"],
            )
        ],
    )

    serialized = context.model_dump_json()
    assert "owner_user_id" not in serialized
    assert "workflow_thread_id" not in serialized
    assert "idempotency_key" not in serialized
    assert "assistant_answer" not in serialized
    assert "SO100001" in serialized


def test_conversation_memory_accepts_legacy_summary_watermark_key() -> None:
    """旧JSON中的summary_through_sequence可读，新序列化只写准确的窗口末端名称。"""

    memory = ConversationMemory.model_validate(
        {"summary_through_sequence": 12},
    )

    assert memory.summary_window_end_sequence == 12
    assert memory.model_dump()["summary_window_end_sequence"] == 12
    assert "summary_through_sequence" not in memory.model_dump()


def test_model_context_rejects_duplicate_or_out_of_order_turns() -> None:
    """并发导致的重复或乱序轮次不能进入上下文解析器。"""

    duplicate_turns = [
        RecentConversationTurn(sequence_number=2, user_message="第一条"),
        RecentConversationTurn(sequence_number=2, user_message="第二条"),
    ]
    with pytest.raises(ValidationError, match="sequence_number"):
        ConversationContext(conversation_id=uuid4(), recent_turns=duplicate_turns)

    out_of_order_turns = [
        RecentConversationTurn(sequence_number=2, user_message="第二轮"),
        RecentConversationTurn(sequence_number=1, user_message="第一轮"),
    ]
    with pytest.raises(ValidationError, match="sequence_number"):
        ConversationContext(conversation_id=uuid4(), recent_turns=out_of_order_turns)
