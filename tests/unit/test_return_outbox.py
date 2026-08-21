"""事务 Outbox 原子提交、至少一次投递、崩溃重放和死信测试。"""

# sqlite3 用触发器故障注入证明 Outbox INSERT 失败时业务 INSERT 会一起回滚。
import sqlite3

# datetime/UTC/timedelta 验证退避后的 pending 事件不会被过早扫描。
from datetime import UTC, datetime, timedelta

# Path 标注 pytest 提供的隔离临时目录。
from pathlib import Path

# pytest 验证数据库主动中止事务时保留原始 SQLite 异常。
import pytest

# 协调器是“Outbox → 审批审计哈希链”的被测应用服务。
from serviceops_agent.application.outbox_reconciler import ReturnOutboxReconciler

# 审计草稿用于先保存决定事件，并模拟“下游成功、Outbox 未标记”崩溃窗口。
from serviceops_agent.domain.audit import ApprovalAuditDraft, ApprovalAuditEventType

# Outbox 元数据和状态是业务事务与协调器之间的强类型契约。
from serviceops_agent.domain.outbox import (
    OutboxStatus,
    ReturnOutboxMetadata,
    build_return_outbox_event_id,
)

# 内存审计仓库提供幂等 append 和哈希链验证。
from serviceops_agent.infrastructure.audit_repository import (
    InMemoryApprovalAuditRepository,
    SQLiteApprovalAuditRepository,
)

# 默认订单仓库包含 user-001 的已签收订单 SO100002。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 两种具体仓库都同时实现退货业务与 Outbox 协议。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    SQLiteReturnRequestRepository,
)


def _metadata(thread_id: str) -> ReturnOutboxMetadata:
    """为单个测试线程构造不含原因、备注和幂等键的可信审批元数据。"""

    return ReturnOutboxMetadata(
        # 每个测试使用不同 thread_id，得到稳定且互不冲突的事件 ID。
        thread_id=thread_id,
        # request_id 模拟首次 API 请求生成的标识。
        request_id=f"request-{thread_id}",
        # actor_id 模拟已验证 JWT sub。
        actor_id="reviewer-outbox-001",
        # 只提供符合长度约束的模拟 jti，不包含真实 Token。
        token_jti="token-jti-outbox-001",
        # 只有批准流程会创建业务提交事件。
        approved=True,
        # 与默认种子订单和后续业务调用保持一致。
        order_id="SO100002",
        # 测试使用固定合法 SHA-256 格式摘要，避免保存申请原因。
        proposal_digest="a" * 64,
        # 测试使用另一固定摘要代表审批备注。
        comment_digest="b" * 64,
    )


def _decision_draft(metadata: ReturnOutboxMetadata) -> ApprovalAuditDraft:
    """把可信元数据转换为业务写入前的审批决定审计事件。"""

    return ApprovalAuditDraft(
        thread_id=metadata.thread_id,
        event_type=ApprovalAuditEventType.DECISION_RECORDED,
        request_id=metadata.request_id,
        actor_id=metadata.actor_id,
        token_jti=metadata.token_jti,
        approved=metadata.approved,
        order_id=metadata.order_id,
        proposal_digest=metadata.proposal_digest,
        comment_digest=metadata.comment_digest,
    )


def _create_approved_return(
    repository: InMemoryReturnRequestRepository | SQLiteReturnRequestRepository,
    metadata: ReturnOutboxMetadata,
    *,
    idempotency_key: str,
) -> str:
    """在同一仓库事务边界创建退货记录和对应待处理事件。"""

    record, is_replay = repository.create_or_get(
        user_id="user-001",
        order_id="SO100002",
        reason="商品尺寸不合适，需要退货",
        idempotency_key=idempotency_key,
        outbox_metadata=metadata,
    )
    # 本辅助函数只用于首次创建场景。
    assert is_replay is False
    return record.return_request_id


def test_memory_repository_commits_business_record_and_outbox_together() -> None:
    """内存实现应在同一锁区间同时出现业务记录与一条 pending 事件。"""

    repository = InMemoryReturnRequestRepository(default_order_repository)
    metadata = _metadata("memory-atomic-001")

    return_request_id = _create_approved_return(
        repository,
        metadata,
        idempotency_key="memory-outbox-atomic-001",
    )

    event_id = build_return_outbox_event_id(metadata.thread_id)
    outbox_event = repository.get_outbox_event(event_id)
    assert repository.count() == 1
    assert repository.count_outbox(OutboxStatus.PENDING) == 1
    assert outbox_event is not None
    assert outbox_event.aggregate_id == return_request_id
    assert outbox_event.payload.return_request_id == return_request_id


def test_sqlite_outbox_insert_failure_rolls_back_business_record(tmp_path: Path) -> None:
    """Outbox 表写入被数据库拒绝时，return_requests INSERT 必须一起回滚。"""

    database_path = tmp_path / "atomic-outbox.db"
    repository = SQLiteReturnRequestRepository(
        database_path=database_path,
        order_repository=default_order_repository,
    )
    # 在建表后注入只针对 Outbox INSERT 的故障；它不会提前阻止业务 INSERT 执行。
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            """
            CREATE TRIGGER fail_outbox_insert
            BEFORE INSERT ON return_outbox_events
            BEGIN
                SELECT RAISE(ABORT, 'injected outbox failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError):
        _create_approved_return(
            repository,
            _metadata("sqlite-rollback-001"),
            idempotency_key="sqlite-outbox-rollback-001",
        )

    # 如果不是同一事务，这里会错误地看到 1 条孤立业务记录。
    assert repository.count() == 0
    assert repository.count_outbox() == 0


def test_reconciler_appends_completed_audit_and_marks_processed() -> None:
    """正常协调应形成决定/完成两节点哈希链，并把 Outbox 标记为 processed。"""

    outbox_repository = InMemoryReturnRequestRepository(default_order_repository)
    audit_repository = InMemoryApprovalAuditRepository()
    metadata = _metadata("reconcile-success-001")
    audit_repository.append(_decision_draft(metadata))
    return_request_id = _create_approved_return(
        outbox_repository,
        metadata,
        idempotency_key="reconcile-success-key-001",
    )

    result = ReturnOutboxReconciler(
        outbox_repository=outbox_repository,
        audit_repository=audit_repository,
    ).reconcile()

    assert result.model_dump() == {
        "scanned": 1,
        "processed": 1,
        "replayed": 0,
        "failed": 0,
        "dead_letter": 0,
    }
    events = audit_repository.list_for_thread(metadata.thread_id)
    assert [event.event_type for event in events] == [
        ApprovalAuditEventType.DECISION_RECORDED,
        ApprovalAuditEventType.WORKFLOW_COMPLETED,
    ]
    assert events[1].return_request_id == return_request_id
    assert audit_repository.verify_thread_chain(metadata.thread_id) is True
    assert outbox_repository.count_outbox(OutboxStatus.PROCESSED) == 1


def test_reconciler_recovers_crash_after_audit_append_without_duplicate() -> None:
    """下游已写但 Outbox 仍 pending 时，重启协调应幂等确认而不是追加第三条事件。"""

    outbox_repository = InMemoryReturnRequestRepository(default_order_repository)
    audit_repository = InMemoryApprovalAuditRepository()
    metadata = _metadata("reconcile-crash-window-001")
    audit_repository.append(_decision_draft(metadata))
    return_request_id = _create_approved_return(
        outbox_repository,
        metadata,
        idempotency_key="reconcile-crash-window-key-001",
    )
    # 模拟协调器 append 成功后、调用 mark_processed 前进程退出。
    audit_repository.append(
        ApprovalAuditDraft(
            **_decision_draft(metadata).model_dump(
                exclude={"event_type", "return_request_id"}
            ),
            event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
            return_request_id=return_request_id,
        )
    )

    result = ReturnOutboxReconciler(
        outbox_repository=outbox_repository,
        audit_repository=audit_repository,
    ).reconcile()

    assert result.replayed == 1
    assert result.processed == 0
    assert len(audit_repository.list_for_thread(metadata.thread_id)) == 2
    assert outbox_repository.count_outbox(OutboxStatus.PROCESSED) == 1


def test_sqlite_pending_event_survives_repository_restart(tmp_path: Path) -> None:
    """业务提交后即使进程退出，新仓库实例也能读取 pending 并完成审计投递。"""

    database_path = tmp_path / "restart-outbox.db"
    metadata = _metadata("sqlite-restart-001")
    first_outbox_repository = SQLiteReturnRequestRepository(
        database_path=database_path,
        order_repository=default_order_repository,
    )
    first_audit_repository = SQLiteApprovalAuditRepository(
        database_path=database_path
    )
    first_audit_repository.append(_decision_draft(metadata))
    _create_approved_return(
        first_outbox_repository,
        metadata,
        idempotency_key="sqlite-restart-outbox-key-001",
    )

    # 重新实例化两类仓库模拟服务重启；没有复用任何 Python 内存字典。
    restarted_outbox_repository = SQLiteReturnRequestRepository(
        database_path=database_path,
        order_repository=default_order_repository,
    )
    restarted_audit_repository = SQLiteApprovalAuditRepository(
        database_path=database_path
    )
    result = ReturnOutboxReconciler(
        outbox_repository=restarted_outbox_repository,
        audit_repository=restarted_audit_repository,
    ).reconcile()

    assert result.processed == 1
    assert restarted_outbox_repository.count() == 1
    assert restarted_outbox_repository.count_outbox(OutboxStatus.PROCESSED) == 1
    assert restarted_audit_repository.verify_thread_chain(metadata.thread_id) is True


def test_failures_back_off_then_enter_dead_letter() -> None:
    """连续三次失败应从 pending 进入 dead_letter，并停止普通到期扫描。"""

    repository = InMemoryReturnRequestRepository(default_order_repository)
    metadata = _metadata("dead-letter-001")
    _create_approved_return(
        repository,
        metadata,
        idempotency_key="dead-letter-outbox-key-001",
    )
    event_id = build_return_outbox_event_id(metadata.thread_id)

    first_failure = repository.record_failure(event_id, error_code="dispatch_error")
    second_failure = repository.record_failure(event_id, error_code="dispatch_error")
    third_failure = repository.record_failure(event_id, error_code="dispatch_error")

    assert first_failure.status == OutboxStatus.PENDING
    assert second_failure.status == OutboxStatus.PENDING
    assert third_failure.status == OutboxStatus.DEAD_LETTER
    assert third_failure.attempts == 3
    # 即使把扫描时间推进到未来，死信也不会被普通补偿器再次选中。
    assert repository.list_pending(
        as_of=datetime.now(UTC) + timedelta(days=1)
    ) == []
