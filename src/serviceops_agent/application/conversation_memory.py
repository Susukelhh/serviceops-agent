"""从持久化终态轮次重建可丢弃、可修复的结构化会话记忆。"""

from collections import Counter
from uuid import UUID

from serviceops_agent.domain.conversation import (
    ConversationMemory,
    ConversationRecord,
    ConversationTurnRecord,
    ConversationTurnStatus,
)
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationRepository,
    ConversationUnavailableError,
    ConversationVersionConflictError,
)


def _append_recent(existing: list[str], additions: list[str]) -> list[str]:
    """按轮次序号和轮内顺序构造最近使用列表。"""

    merged = list(existing)
    for item in additions:
        if item in merged:
            merged.remove(item)
        merged.append(item)
    return merged[-10:]


SUMMARY_INTENTS = frozenset(
    {"faq", "human_handoff", "order_status", "return_request"}
)


def _summary_field_value(entries: list[str], *, budget: int) -> str:
    """在字段预算内保留最新的完整条目，绝不截断单个标识。"""

    if not entries:
        return "无"
    for omitted_count in range(len(entries)):
        retained = entries[omitted_count:]
        omitted_prefix = f"已省略{omitted_count}项、" if omitted_count else ""
        candidate = omitted_prefix + "、".join(retained)
        if len(candidate) <= budget:
            return candidate
    return "已省略"


def _summary_value_budgets(
    *,
    entries_by_field: list[list[str]],
    available_chars: int,
) -> list[int]:
    """公平分配字段预算，空字段不占用其他字段的剩余空间。"""

    budgets = [len("已省略") if entries else len("无") for entries in entries_by_field]
    if sum(budgets) > available_chars:
        raise ValueError("摘要字段的最小预算不足")
    desired = [
        max(current, len("、".join(entries)))
        for current, entries in zip(budgets, entries_by_field, strict=True)
    ]
    remaining = available_chars - sum(budgets)
    while remaining and any(
        current < target for current, target in zip(budgets, desired, strict=True)
    ):
        for index, target in enumerate(desired):
            if remaining == 0:
                break
            if budgets[index] < target:
                budgets[index] += 1
                remaining -= 1
    return budgets


def _build_bounded_summary(
    *,
    completed_turns: list[ConversationTurnRecord],
    max_chars: int,
) -> str:
    """只从已完成轮次的有限结构化字段构造摘要。"""

    intent_counts = Counter(
        turn.intent
        for turn in completed_turns
        if turn.intent is not None and turn.intent in SUMMARY_INTENTS
    )
    intent_entries = [
        f"{intent}:{count}" for intent, count in sorted(intent_counts.items())
    ]
    summary_order_ids: list[str] = []
    summary_document_ids: set[str] = set()
    for turn in completed_turns:
        summary_order_ids = _append_recent(
            summary_order_ids,
            turn.verified_order_ids,
        )
        summary_document_ids.update(turn.cited_document_ids)

    field_entries = [
        intent_entries,
        summary_order_ids,
        [f"{len(summary_document_ids)}项"] if summary_document_ids else [],
    ]
    # 文档ID的Schema只约束长度，可能被上游命名成邮箱或手机号；摘要只保留数量。
    field_labels = ["意图计数", "最近已验证订单", "引用来源数"]
    prefix = f"结构化安全摘要：窗口内已完成{len(completed_turns)}轮"
    fixed_text = prefix + "".join(f"；{label}=" for label in field_labels) + "。"
    budgets = _summary_value_budgets(
        entries_by_field=field_entries,
        available_chars=max_chars - len(fixed_text),
    )
    values = [
        _summary_field_value(entries, budget=budget)
        for entries, budget in zip(field_entries, budgets, strict=True)
    ]
    summary = prefix + "".join(
        f"；{label}={value}"
        for label, value in zip(field_labels, values, strict=True)
    ) + "。"
    if len(summary) > max_chars:
        raise ValueError("摘要构造结果超出字符预算")
    return summary


def _rebuilt_memory(
    *,
    existing: ConversationMemory,
    turns: list[ConversationTurnRecord],
    summary_after_turns: int,
    summary_max_chars: int,
) -> ConversationMemory:
    """从最近终态源记录确定性重建，使乱序完成与重放最终收敛。"""

    ordered_turns = sorted(
        turns,
        key=lambda turn: (turn.sequence_number, str(turn.turn_id)),
    )
    focus_turns = [
        turn
        for turn in ordered_turns
        if (
            turn.status
            in {ConversationTurnStatus.COMPLETED, ConversationTurnStatus.WAITING_APPROVAL}
            # 审批CAS会暂时把已索引的waiting轮次改为running；完整结果字段用于区分初始执行。
            or (
                turn.status == ConversationTurnStatus.RUNNING
                and turn.standalone_question is not None
                and turn.assistant_answer is not None
                and turn.intent is not None
            )
        )
    ]
    if not focus_turns:
        return ConversationMemory(
            memory_version=existing.memory_version + 1,
        )

    recent_order_ids: list[str] = []
    recent_document_ids: list[str] = []
    for turn in focus_turns:
        recent_order_ids = _append_recent(recent_order_ids, turn.verified_order_ids)
        recent_document_ids = _append_recent(
            recent_document_ids,
            turn.cited_document_ids,
        )

    latest = focus_turns[-1]
    # 活动订单只属于最新订单/退货主题；切换FAQ/人工主题或出现多单歧义时主动清空。
    active_order_id = None
    if latest.intent in {"order_status", "return_request"} and len(
        latest.verified_order_ids
    ) == 1:
        active_order_id = latest.verified_order_ids[0]

    bounded_summary = None
    summary_window_end_sequence = 0
    completed_turns = [
        turn
        for turn in focus_turns
        if turn.status == ConversationTurnStatus.COMPLETED
    ]
    if len(completed_turns) > summary_after_turns:
        # WAITING_APPROVAL和认领中的RUNNING只可影响当前焦点，不沉淀进长期摘要。
        bounded_summary = _build_bounded_summary(
            completed_turns=completed_turns,
            max_chars=summary_max_chars,
        )
        summary_window_end_sequence = completed_turns[-1].sequence_number

    return ConversationMemory(
        memory_version=existing.memory_version + 1,
        current_topic=latest.intent,
        active_order_id=active_order_id,
        recent_order_ids=recent_order_ids,
        recent_document_ids=recent_document_ids,
        last_intent=latest.intent,
        last_processed_sequence=latest.sequence_number,
        bounded_summary=bounded_summary,
        summary_window_end_sequence=summary_window_end_sequence,
    )


def rebuild_conversation_memory(
    *,
    repository: ConversationRepository,
    conversation_id: UUID,
    owner_user_id: str,
    summary_after_turns: int = 6,
    summary_max_chars: int = 1000,
    max_attempts: int = 5,
) -> ConversationRecord:
    """使用乐观并发从源轮次重建记忆；冲突后重新读取全部输入。"""

    if max_attempts < 1:
        raise ValueError("max_attempts必须至少为1")
    if not 2 <= summary_after_turns <= 50:
        raise ValueError("summary_after_turns必须位于2到50")
    if not 200 <= summary_max_chars <= 2000:
        raise ValueError("summary_max_chars必须位于200到2000")
    last_conflict: ConversationVersionConflictError | None = None
    for _ in range(max_attempts):
        conversation = repository.get_conversation_for_owner(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
        )
        if conversation is None:
            raise ConversationUnavailableError
        # 最近50轮足以确定最新焦点，并重建上限各10项的两个有限集合。
        turns = repository.list_recent_turns(
            conversation_id=conversation_id,
            owner_user_id=owner_user_id,
            limit=50,
        )
        next_memory = _rebuilt_memory(
            existing=conversation.memory,
            turns=turns,
            summary_after_turns=summary_after_turns,
            summary_max_chars=summary_max_chars,
        )
        if next_memory is conversation.memory:
            return conversation
        # 重放同一源轮次时，语义内容完全一致，不制造无意义版本。
        if next_memory.model_copy(
            update={"memory_version": conversation.memory.memory_version}
        ) == conversation.memory:
            return conversation
        try:
            return repository.update_memory(
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                expected_version=conversation.memory.memory_version,
                memory=next_memory,
            )
        except ConversationVersionConflictError as error:
            last_conflict = error
    assert last_conflict is not None
    raise last_conflict
