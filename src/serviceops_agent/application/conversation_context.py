"""从持久化轮次构建有界可信上下文，并确定性解析常见追问。"""

import re
from uuid import UUID

from serviceops_agent.domain.conversation import (
    ConversationContext,
    ConversationRecord,
    ConversationTurnStatus,
    FollowUpResolution,
    FollowUpResolutionReason,
    RecentConversationTurn,
)
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationRepository,
    ConversationUnavailableError,
)

# 订单号只从当前消息或仓库中已验证字段读取，不能从历史自然语言或模型答案中猜取。
ORDER_ID_PATTERN = re.compile(r"(?i)(?<![A-Z0-9])SO\d{6}(?![A-Z0-9])")

# 这些短语表明用户可能省略了订单主语；是否绑定仍取决于可信候选是否唯一。
ORDER_REFERENCE_MARKERS = (
    "这个订单",
    "那个订单",
    "该订单",
    "这单",
    "那单",
    "它",
    "这个包裹",
    "那个包裹",
    "该包裹",
)

# 一般主题追问只继承上一轮独立问题，不复制历史助手答案或隐藏状态。
TOPIC_FOLLOW_UP_MARKERS = (
    "那",
    "那么",
    "还有",
    "另外",
    "继续",
    "刚才",
    "上面",
    "上述",
    "这个",
    "为什么",
    "呢",
)


def build_conversation_context(
    *,
    repository: ConversationRepository,
    conversation: ConversationRecord,
    owner_user_id: str,
    before_sequence: int,
) -> ConversationContext:
    """构造当前轮之前最多六个可用轮次的安全投影。"""

    # 最多读取仓库协议允许的50轮，再过滤失败/并发中的轮次，最后取最近六轮。
    stored_turns = repository.list_recent_turns(
        conversation_id=conversation.conversation_id,
        owner_user_id=owner_user_id,
        limit=50,
    )
    eligible_turns = [
        turn
        for turn in stored_turns
        if turn.sequence_number < before_sequence
        and turn.status
        in {ConversationTurnStatus.COMPLETED, ConversationTurnStatus.WAITING_APPROVAL}
    ][-6:]
    return ConversationContext(
        conversation_id=conversation.conversation_id,
        memory=conversation.memory,
        recent_turns=[
            RecentConversationTurn(
                sequence_number=turn.sequence_number,
                user_message=turn.user_message,
                standalone_question=turn.standalone_question,
                intent=turn.intent,
                verified_order_ids=turn.verified_order_ids,
                cited_document_ids=turn.cited_document_ids,
            )
            for turn in eligible_turns
        ],
    )


def _explicit_order_ids(message: str) -> list[str]:
    """按首次出现顺序提取当前消息中的规范订单号。"""

    return list(
        dict.fromkeys(
            match.group(0).upper() for match in ORDER_ID_PATTERN.finditer(message)
        )
    )


def _latest_verified_order_candidates(
    context: ConversationContext,
) -> tuple[list[str], int | None, bool]:
    """优先返回最近一轮工具验证过的订单集合，其次使用结构化活动订单。"""

    if context.memory.active_order_id is not None:
        return [context.memory.active_order_id], None, True
    # 记忆焦点为空时只检查紧邻轮次；不能跨过FAQ/人工主题绑定更早订单。
    if context.recent_turns:
        latest = context.recent_turns[-1]
        if (
            latest.intent in {"order_status", "return_request"}
            and latest.verified_order_ids
        ):
            return latest.verified_order_ids, latest.sequence_number, False
    return [], None, False


def _bounded_question(prefix: str, message: str) -> str:
    """组合上下文与当前问题，同时保持领域契约的4000字符上限。"""

    available = 4000 - len(prefix)
    return prefix + message[: max(available, 0)]


def resolve_follow_up(
    *,
    message: str,
    context: ConversationContext,
) -> FollowUpResolution:
    """只使用白名单上下文解析订单指代和常见主题省略。"""

    normalized_message = message.strip()
    explicit_order_ids = _explicit_order_ids(normalized_message)
    if explicit_order_ids:
        return FollowUpResolution(
            standalone_question=normalized_message,
            reason=FollowUpResolutionReason.EXPLICIT_REFERENCE,
            used_context=False,
            referenced_order_ids=explicit_order_ids,
        )

    has_order_reference = any(
        marker in normalized_message for marker in ORDER_REFERENCE_MARKERS
    )
    if has_order_reference:
        candidates, source_sequence, source_memory = _latest_verified_order_candidates(context)
        if len(candidates) == 1 and (source_sequence is not None or source_memory):
            order_id = candidates[0]
            return FollowUpResolution(
                standalone_question=_bounded_question(
                    f"关于订单 {order_id}，",
                    normalized_message,
                ),
                reason=FollowUpResolutionReason.VERIFIED_ORDER_REFERENCE,
                used_context=True,
                referenced_order_ids=[order_id],
                source_turn_sequence=source_sequence,
                source_memory=source_memory,
            )
        if len(candidates) > 1 and (source_sequence is not None or source_memory):
            # 不把候选订单号拼入执行问题，防止下游工具误选其中之一。
            return FollowUpResolution(
                standalone_question=_bounded_question(
                    "订单追问存在多个候选，请先要求用户明确订单号：",
                    normalized_message,
                ),
                reason=FollowUpResolutionReason.AMBIGUOUS_ORDER_REFERENCE,
                used_context=True,
                needs_clarification=True,
                source_turn_sequence=source_sequence,
                source_memory=source_memory,
            )

    if context.recent_turns and any(
        marker in normalized_message for marker in TOPIC_FOLLOW_UP_MARKERS
    ):
        previous = context.recent_turns[-1]
        previous_question = previous.standalone_question or previous.user_message
        # 历史问题作为清晰标注的数据片段；上下文投影根本不包含历史回答。
        prefix = f"延续上一轮问题“{previous_question[:1000]}”，本轮追问："
        return FollowUpResolution(
            standalone_question=_bounded_question(prefix, normalized_message),
            reason=FollowUpResolutionReason.PREVIOUS_TOPIC_REFERENCE,
            used_context=True,
            source_turn_sequence=previous.sequence_number,
        )

    return FollowUpResolution(
        standalone_question=normalized_message,
        reason=FollowUpResolutionReason.INDEPENDENT_QUESTION,
        used_context=False,
    )


def prepare_conversation_input(
    *,
    repository: ConversationRepository,
    conversation_id: UUID,
    owner_user_id: str,
    before_sequence: int,
    message: str,
) -> FollowUpResolution:
    """读取会话并完成一轮解析；不存在和越权仍由调用方统一映射。"""

    conversation = repository.get_conversation_for_owner(
        conversation_id=conversation_id,
        owner_user_id=owner_user_id,
    )
    if conversation is None:
        # 与仓库写入口使用同一异常，使API不泄漏所有权差异。
        raise ConversationUnavailableError
    context = build_conversation_context(
        repository=repository,
        conversation=conversation,
        owner_user_id=owner_user_id,
        before_sequence=before_sequence,
    )
    return resolve_follow_up(message=message, context=context)
