"""增加会话轮次执行租约与 fencing generation。

Revision ID: 20260829_0003
Revises: 20260829_0002
Create Date: 2026-08-29

租约行不保存用户输入或模型输出，只保存恢复并发控制需要的低敏元数据。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0003"
down_revision: str | None = "20260829_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建每轮唯一的执行租约和陈旧租约扫描索引。"""

    op.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('serviceops-business-migrations', 0))"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_execution_leases (
            turn_id UUID PRIMARY KEY
                REFERENCES conversation_turns(turn_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('initial', 'approval_resume')),
            state TEXT NOT NULL CHECK (
                state IN ('active', 'released', 'revoked', 'reconciliation_required')
            ),
            claim_token UUID NOT NULL UNIQUE,
            fence_generation BIGINT NOT NULL CHECK (fence_generation >= 1),
            decision_audit_event_id TEXT
                REFERENCES approval_audit_events(audit_event_id),
            claimed_at TIMESTAMPTZ NOT NULL,
            heartbeat_at TIMESTAMPTZ NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL,
            CHECK (heartbeat_at >= claimed_at),
            CHECK (state <> 'active' OR lease_expires_at > heartbeat_at),
            CHECK (
                (kind = 'initial' AND decision_audit_event_id IS NULL)
                OR
                (kind = 'approval_resume' AND decision_audit_event_id IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_execution_leases_state_expiry
        ON conversation_execution_leases (state, lease_expires_at)
        """
    )


def downgrade() -> None:
    """禁止自动删除执行所有权与故障恢复证据。"""

    raise RuntimeError("执行租约迁移禁止自动 downgrade；请使用受控备份恢复流程")
