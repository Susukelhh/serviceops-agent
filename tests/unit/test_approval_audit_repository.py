"""审批审计仓库的哈希链、幂等、冲突、SQLite 保护和跨实例测试。"""

# sqlite3 用于模拟具有数据库文件权限的管理员绕过只追加触发器后篡改字段。
import sqlite3

# Path 标注 pytest 提供的隔离临时目录。
from pathlib import Path

# pytest 提供异常断言。
import pytest

# 审计草稿和有限事件类型是仓库的强类型输入。
from serviceops_agent.domain.audit import ApprovalAuditDraft, ApprovalAuditEventType

# 两种仓库实现、冲突异常是本文件的测试目标。
from serviceops_agent.infrastructure.audit_repository import (
    ApprovalAuditConflictError,
    InMemoryApprovalAuditRepository,
    SQLiteApprovalAuditRepository,
)


def _draft(
    *,
    event_type: ApprovalAuditEventType,
    approved: bool = True,
    return_request_id: str | None = None,
    actor_id: str = "reviewer-001",
    token_jti: str = "token-jti-001",
) -> ApprovalAuditDraft:
    """为单元测试构造不含业务原文的合法审计草稿。"""

    # 摘要使用固定 64 位十六进制值，使测试只关注仓库链式行为。
    return ApprovalAuditDraft(
        # 同一 thread 让决定和结果进入同一条链。
        thread_id="audit-thread-001",
        # 调用方选择决定或结果事件。
        event_type=event_type,
        # 原 Agent 请求标识。
        request_id="audit-request-001",
        # 可信审批主体。
        actor_id=actor_id,
        # 只记录 jti，不需要真实 JWT。
        token_jti=token_jti,
        # 审批布尔决定。
        approved=approved,
        # 合法测试订单号。
        order_id="SO100002",
        # 固定合法草案摘要。
        proposal_digest="a" * 64,
        # 固定合法备注摘要。
        comment_digest="b" * 64,
        # 只有 completed 事件会传入申请编号。
        return_request_id=return_request_id,
    )


def test_memory_repository_builds_and_verifies_two_event_chain() -> None:
    """内存仓库应把决定和成功结果连接成两节点有效哈希链。"""

    # Arrange：创建隔离的进程内仓库。
    repository = InMemoryApprovalAuditRepository()

    # Act：先追加审批决定。
    decision_event, decision_replay = repository.append(
        _draft(event_type=ApprovalAuditEventType.DECISION_RECORDED)
    )
    # Act：再追加写工具成功结果。
    result_event, result_replay = repository.append(
        _draft(
            event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
            return_request_id="RR-ABCDEF123456",
        )
    )

    # Assert：两次都是首次写入。
    assert decision_replay is False
    assert result_replay is False
    # Assert：位置从一开始连续递增。
    assert decision_event.chain_position == 1
    assert result_event.chain_position == 2
    # Assert：第二条明确引用第一条哈希。
    assert result_event.previous_event_hash == decision_event.event_hash
    # Assert：重新计算完整链结果为真。
    assert repository.verify_thread_chain("audit-thread-001") is True


def test_memory_repository_replays_same_decision_after_token_renewal() -> None:
    """同一主体和业务决定在 Token 续期后重试应返回原事件，不增加链长度。"""

    # Arrange：先用第一枚 Token jti 记录决定。
    repository = InMemoryApprovalAuditRepository()
    original_event, _ = repository.append(
        _draft(event_type=ApprovalAuditEventType.DECISION_RECORDED)
    )

    # Act：同一审批主体持新 Token 重试完全相同的业务决定。
    replayed_event, is_replay = repository.append(
        _draft(
            event_type=ApprovalAuditEventType.DECISION_RECORDED,
            token_jti="renewed-token-jti-002",
        )
    )

    # Assert：返回第一次事件，保留实际首次提交所用 jti。
    assert is_replay is True
    assert replayed_event.audit_event_id == original_event.audit_event_id
    assert replayed_event.token_jti == "token-jti-001"
    # Assert：重试不会伪造第二条决定事件。
    assert len(repository.list_for_thread("audit-thread-001")) == 1


def test_memory_repository_rejects_different_actor_for_same_decision_event() -> None:
    """同一线程不能把既有决定静默替换成另一审批主体。"""

    # Arrange：第一位审批人已经记录决定。
    repository = InMemoryApprovalAuditRepository()
    repository.append(_draft(event_type=ApprovalAuditEventType.DECISION_RECORDED))

    # Act/Assert：第二位审批人的同类型事件属于冲突而不是幂等重放。
    with pytest.raises(ApprovalAuditConflictError):
        repository.append(
            _draft(
                event_type=ApprovalAuditEventType.DECISION_RECORDED,
                actor_id="reviewer-002",
            )
        )


def test_sqlite_repository_persists_chain_and_blocks_normal_mutation(
    tmp_path: Path,
) -> None:
    """SQLite 新实例应读取原链，且普通 UPDATE/DELETE 被触发器拒绝。"""

    # Arrange：所有数据库写入 pytest 临时目录，不污染项目运行数据。
    database_path = tmp_path / "audit.sqlite3"
    first_repository = SQLiteApprovalAuditRepository(database_path=database_path)
    # Act：第一实例追加两条事件。
    first_repository.append(
        _draft(event_type=ApprovalAuditEventType.DECISION_RECORDED)
    )
    first_repository.append(
        _draft(
            event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
            return_request_id="RR-ABCDEF123456",
        )
    )

    # Act：创建全新仓库对象模拟服务重启。
    restarted_repository = SQLiteApprovalAuditRepository(database_path=database_path)
    persisted_events = restarted_repository.list_for_thread("audit-thread-001")

    # Assert：两条事件和有效链都跨实例存在。
    assert len(persisted_events) == 2
    assert restarted_repository.verify_thread_chain("audit-thread-001") is True

    # Arrange：直接连接数据库，模拟绕过仓库接口的普通写 SQL。
    connection = sqlite3.connect(str(database_path), isolation_level=None)
    try:
        # Act/Assert：只追加触发器拒绝覆盖历史审批人。
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE approval_audit_events SET actor_id = ? WHERE chain_position = 1",
                ("attacker-001",),
            )
        # Act/Assert：只追加触发器也拒绝删除历史事件。
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM approval_audit_events WHERE chain_position = 1"
            )
    finally:
        # 及时关闭测试连接，避免 Windows 临时文件被句柄占用。
        connection.close()


def test_sqlite_hash_chain_detects_admin_tampering_after_trigger_bypass(
    tmp_path: Path,
) -> None:
    """管理员删除保护触发器并改写合法格式字段后，重新计算链应失败。"""

    # Arrange：建立包含决定和结果的有效 SQLite 链。
    database_path = tmp_path / "tampered-audit.sqlite3"
    repository = SQLiteApprovalAuditRepository(database_path=database_path)
    repository.append(_draft(event_type=ApprovalAuditEventType.DECISION_RECORDED))
    repository.append(
        _draft(
            event_type=ApprovalAuditEventType.WORKFLOW_REJECTED,
            approved=False,
        )
    )
    # 篡改前完整性必须为真，避免测试误报。
    assert repository.verify_thread_chain("audit-thread-001") is True

    # Act：模拟拥有数据库管理权限的攻击者先删除 UPDATE 保护，再修改主体但不重算哈希。
    connection = sqlite3.connect(str(database_path), isolation_level=None)
    try:
        connection.execute("DROP TRIGGER prevent_approval_audit_update")
        connection.execute(
            "UPDATE approval_audit_events SET actor_id = ? WHERE chain_position = 1",
            ("attacker-001",),
        )
    finally:
        connection.close()

    # Assert：字段仍符合 Schema，但哈希重算不再匹配，篡改被检测。
    assert repository.verify_thread_chain("audit-thread-001") is False
