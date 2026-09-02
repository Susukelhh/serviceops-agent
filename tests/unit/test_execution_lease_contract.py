"""第47步执行租约领域、配置和迁移契约测试。"""

from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

import pytest
from pydantic import ValidationError

from serviceops_agent.config.settings import Settings
from serviceops_agent.domain.conversation import (
    ConversationExecutionLease,
    ConversationExecutionRecoveryResult,
    ExecutionKind,
    ExecutionLeaseState,
)


def _lease_payload() -> dict[str, object]:
    """返回可以被各反例测试独立覆盖的合法初始租约。"""

    claimed_at = datetime.now(UTC)
    return {
        "turn_id": uuid4(),
        "kind": ExecutionKind.INITIAL,
        "state": ExecutionLeaseState.ACTIVE,
        "claim_token": uuid4(),
        "fence_generation": 1,
        "claimed_at": claimed_at,
        "heartbeat_at": claimed_at + timedelta(seconds=20),
        "lease_expires_at": claimed_at + timedelta(seconds=90),
    }


def test_execution_lease_hides_claim_token_from_repr() -> None:
    """日志常用repr不能暴露持有者凭证，但内部序列化仍保留强类型字段。"""

    payload = _lease_payload()
    lease = ConversationExecutionLease.model_validate(payload)

    assert "claim_token" not in repr(lease)
    assert lease.claim_token == payload["claim_token"]
    assert lease.fence_generation == 1


def test_execution_lease_requires_correct_decision_audit_source() -> None:
    """初始执行与审批恢复不能混用或省略审批决定证据。"""

    initial_with_decision = _lease_payload() | {
        "decision_audit_event_id": str(uuid4()),
    }
    with pytest.raises(ValidationError, match="不能包含审批决定"):
        ConversationExecutionLease.model_validate(initial_with_decision)

    approval_without_decision = _lease_payload() | {
        "kind": ExecutionKind.APPROVAL_RESUME,
    }
    with pytest.raises(ValidationError, match="必须包含审批决定"):
        ConversationExecutionLease.model_validate(approval_without_decision)

    approval = ConversationExecutionLease.model_validate(
        approval_without_decision
        | {"decision_audit_event_id": str(uuid4())},
    )
    assert approval.kind == ExecutionKind.APPROVAL_RESUME


@pytest.mark.parametrize(
    ("field_name", "value", "error"),
    [
        ("claimed_at", datetime.now(), "必须包含时区"),
        ("heartbeat_at", datetime.now(), "必须包含时区"),
        ("lease_expires_at", datetime.now(), "必须包含时区"),
    ],
)
def test_execution_lease_requires_timezone_aware_timestamps(
    field_name: str,
    value: datetime,
    error: str,
) -> None:
    """跨实例租约时间不接受依赖服务器本地时区的朴素datetime。"""

    with pytest.raises(ValidationError, match=error):
        ConversationExecutionLease.model_validate(
            _lease_payload() | {field_name: value},
        )


def test_active_execution_lease_requires_ordered_heartbeat_and_expiry() -> None:
    """活动租约不能用倒退心跳或非正续租窗口伪装成合法所有权。"""

    payload = _lease_payload()
    claimed_at = payload["claimed_at"]
    assert isinstance(claimed_at, datetime)
    with pytest.raises(ValidationError, match="heartbeat_at"):
        ConversationExecutionLease.model_validate(
            payload | {"heartbeat_at": claimed_at - timedelta(seconds=1)},
        )

    heartbeat_at = payload["heartbeat_at"]
    assert isinstance(heartbeat_at, datetime)
    with pytest.raises(ValidationError, match="到期时间"):
        ConversationExecutionLease.model_validate(
            payload | {"lease_expires_at": heartbeat_at},
        )


def test_execution_lease_settings_defaults_and_heartbeat_ratio() -> None:
    """默认至少容纳三次心跳，错误组合在应用启动前失败。"""

    settings = Settings()

    assert settings.lease_duration_seconds == 90
    assert settings.heartbeat_interval_seconds == 20
    assert settings.stale_grace_seconds == 30
    assert settings.accepted_stale_seconds == 60

    with pytest.raises(ValidationError, match="至少容纳三次心跳"):
        Settings(
            lease_duration_seconds=59,
            heartbeat_interval_seconds=20,
        )


def test_execution_recovery_result_is_bounded_low_sensitive_aggregate() -> None:
    """恢复结果只公开互斥计数，分类总数不能虚报超过扫描数。"""

    result = ConversationExecutionRecoveryResult(
        scanned_count=5,
        accepted_failed_count=1,
        initial_failed_count=1,
        approval_quarantined_count=1,
        legacy_manual_review_count=1,
    )

    assert result.model_dump() == {
        "scanned_count": 5,
        "accepted_failed_count": 1,
        "initial_failed_count": 1,
        "approval_quarantined_count": 1,
        "legacy_manual_review_count": 1,
    }
    with pytest.raises(ValidationError, match="分类合计不能超过扫描数量"):
        ConversationExecutionRecoveryResult(
            scanned_count=1,
            accepted_failed_count=1,
            initial_failed_count=1,
            approval_quarantined_count=0,
            legacy_manual_review_count=0,
        )


def test_execution_lease_migration_extends_chain_and_enforces_database_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL DDL重复领域关键约束并提供陈旧扫描索引。"""

    migration = import_module(
        "serviceops_agent.migrations.versions.20260829_0003_execution_leases"
    )
    statements: list[str] = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()
    ddl = "\n".join(statements)

    assert migration.revision == "20260829_0003"
    assert migration.down_revision == "20260829_0002"
    assert "conversation_execution_leases" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "fence_generation >= 1" in ddl
    assert "kind = 'initial' AND decision_audit_event_id IS NULL" in ddl
    assert "kind = 'approval_resume' AND decision_audit_event_id IS NOT NULL" in ddl
    assert "REFERENCES approval_audit_events(audit_event_id)" in ddl
    assert "(state, lease_expires_at)" in ddl

    with pytest.raises(RuntimeError, match="禁止自动 downgrade"):
        migration.downgrade()
