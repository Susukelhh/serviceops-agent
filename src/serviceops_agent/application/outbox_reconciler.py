"""把事务 Outbox 事件至少一次投递到审批审计哈希链的协调器。"""

# logging 只记录稳定事件 ID 和有限错误码，不记录载荷、备注、原因或 Token。
import logging

# 审批完成草稿由强类型 Outbox 载荷确定性构造。
from serviceops_agent.domain.audit import ApprovalAuditDraft, ApprovalAuditEventType

# 批次结果与状态用于对外公开低敏统计并区分死信。
from serviceops_agent.domain.outbox import OutboxStatus, ReconciliationBatchResult

# 审计仓库 append 自带稳定事件 ID 和幂等比较，是“至少一次”消费的关键。
from serviceops_agent.infrastructure.audit_repository import (
    ApprovalAuditConflictError,
    ApprovalAuditRepository,
)

# Outbox 协议允许同一协调逻辑运行在内存和 SQLite 后端。
from serviceops_agent.infrastructure.outbox_repository import ReturnOutboxRepository

# 指标只记录有限 outcome 标签，不使用 event/thread/user 等高基数标识。
from serviceops_agent.observability.telemetry import record_outbox_dispatch

# 模块日志器禁止输出 payload 或异常正文。
logger = logging.getLogger(__name__)


class ReturnOutboxReconciler:
    """协调业务 Outbox 与只追加审计仓库，并安全处理重复投递。"""

    def __init__(
        self,
        *,
        outbox_repository: ReturnOutboxRepository,
        audit_repository: ApprovalAuditRepository,
        max_attempts: int = 3,
    ) -> None:
        """绑定两个持久化边界和单条事件最大失败次数。"""

        # 1 到 20 的边界与仓库保持一致，避免配置错误形成永久快速重试。
        if not 1 <= max_attempts <= 20:
            raise ValueError("Outbox max_attempts 必须位于 1 到 20")
        # 事务 Outbox 仓库负责扫描和状态推进。
        self._outbox_repository = outbox_repository
        # 审计仓库负责哈希链追加与幂等冲突判断。
        self._audit_repository = audit_repository
        # 达到上限后事件进入 dead_letter，不再被普通扫描选中。
        self._max_attempts = max_attempts

    def _validate_audit_precondition(self, draft: ApprovalAuditDraft) -> None:
        """确认完成事件之前已有同语义决定，且没有互斥终态。"""

        # API 约定决定事件一定先于业务写；缺失说明数据被绕过或历史迁移不完整。
        events = self._audit_repository.list_for_thread(draft.thread_id)
        decision_event = next(
            (
                event
                for event in events
                if event.event_type == ApprovalAuditEventType.DECISION_RECORDED
            ),
            None,
        )
        if decision_event is None:
            raise ApprovalAuditConflictError
        # Token 续期不改变同一主体重试语义；其他决定字段必须与 Outbox 完全一致。
        comparable_fields = set(ApprovalAuditDraft.model_fields) - {
            "event_type",
            "token_jti",
            "return_request_id",
        }
        decision_payload = decision_event.model_dump(
            mode="json",
            include=comparable_fields,
        )
        completed_payload = draft.model_dump(
            mode="json",
            include=comparable_fields,
        )
        if decision_payload != completed_payload:
            raise ApprovalAuditConflictError
        # 已经记录 rejected/failed 后不能再追加 completed；completed 自身则允许幂等确认。
        if any(
            event.event_type
            in {
                ApprovalAuditEventType.WORKFLOW_REJECTED,
                ApprovalAuditEventType.WORKFLOW_FAILED,
            }
            for event in events
        ):
            raise ApprovalAuditConflictError

    def reconcile(
        self,
        *,
        limit: int = 100,
        thread_id: str | None = None,
    ) -> ReconciliationBatchResult:
        """投递一批到期事件；同一审计事件重复出现时安全确认完成。"""

        # 仓库负责 limit 和到期时间过滤，并返回强类型快照。
        pending_events = self._outbox_repository.list_pending(
            limit=limit,
            thread_id=thread_id,
        )
        # 以下计数只存在于当前批次，不保存任何用户或业务正文。
        processed = 0
        replayed = 0
        failed = 0
        dead_letter = 0

        # 单条失败不能阻断同一批次的其他独立事件。
        for outbox_event in pending_events:
            # payload 已由仓库读取边界通过 Pydantic 重新校验。
            payload = outbox_event.payload
            # Outbox 事件只映射成 workflow_completed，不接受自由事件类型。
            audit_draft = ApprovalAuditDraft(
                thread_id=payload.thread_id,
                event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
                request_id=payload.request_id,
                actor_id=payload.actor_id,
                token_jti=payload.token_jti,
                approved=payload.approved,
                order_id=payload.order_id,
                proposal_digest=payload.proposal_digest,
                comment_digest=payload.comment_digest,
                return_request_id=payload.return_request_id,
            )
            try:
                # 防止孤立 completed 或与拒绝/失败终态并存，先验证链上的决定前置条件。
                self._validate_audit_precondition(audit_draft)
                # append 可能新增，也可能发现同语义事件已在上次崩溃前成功写入。
                _, was_replayed = self._audit_repository.append(audit_draft)
                # 只有下游确认后才推进 Outbox；此处失败会保留至少一次重试语义。
                self._outbox_repository.mark_processed(outbox_event.event_id)
                if was_replayed:
                    replayed += 1
                    record_outbox_dispatch(outcome="replayed")
                else:
                    processed += 1
                    record_outbox_dispatch(outcome="processed")
            except ApprovalAuditConflictError:
                # 相同线程完成事件语义不同属于安全冲突，不能覆盖既有哈希链。
                failed_record = self._outbox_repository.record_failure(
                    outbox_event.event_id,
                    error_code="audit_conflict",
                    max_attempts=self._max_attempts,
                )
                if failed_record.status == OutboxStatus.DEAD_LETTER:
                    dead_letter += 1
                    record_outbox_dispatch(outcome="dead_letter")
                else:
                    failed += 1
                    record_outbox_dispatch(outcome="failed")
                logger.warning(
                    "Outbox 审计语义冲突: event_id=%s",
                    outbox_event.event_id,
                    extra={"operation": "outbox_dispatch", "failure_code": "audit_conflict"},
                )
            except Exception as error:
                # 网络、磁盘或“下游成功但标记前崩溃”都只记录异常类型并等待重试。
                failed_record = self._outbox_repository.record_failure(
                    outbox_event.event_id,
                    error_code="dispatch_error",
                    max_attempts=self._max_attempts,
                )
                if failed_record.status == OutboxStatus.DEAD_LETTER:
                    dead_letter += 1
                    record_outbox_dispatch(outcome="dead_letter")
                else:
                    failed += 1
                    record_outbox_dispatch(outcome="failed")
                logger.warning(
                    "Outbox 投递失败: event_id=%s cause_type=%s",
                    outbox_event.event_id,
                    type(error).__name__,
                    extra={"operation": "outbox_dispatch", "failure_code": "dispatch_error"},
                )

        # 返回的只是有限计数，可安全用于内部运维接口和本地演示。
        return ReconciliationBatchResult(
            scanned=len(pending_events),
            processed=processed,
            replayed=replayed,
            failed=failed,
            dead_letter=dead_letter,
        )
