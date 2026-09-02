"""增加多轮会话与独立业务轮次表。

Revision ID: 20260829_0002
Revises: 20260821_0001
Create Date: 2026-08-29

会话表只保存用户可见轮次和有限结构化记忆；LangGraph执行快照继续由官方Checkpointer表管理。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0002"
down_revision: str | None = "20260821_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建会话、轮次、所有权、幂等和顺序约束。"""

    op.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('serviceops-business-migrations', 0))"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id UUID PRIMARY KEY,
            owner_user_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'closed', 'expired')),
            memory_json JSONB NOT NULL,
            memory_version INTEGER NOT NULL CHECK (memory_version >= 0),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            CHECK (updated_at >= created_at),
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
        ON conversations (owner_user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversations_expiry
        ON conversations (status, expires_at)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_turns (
            turn_id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversations(conversation_id),
            workflow_thread_id UUID NOT NULL UNIQUE,
            sequence_number INTEGER NOT NULL CHECK (sequence_number >= 1),
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('accepted', 'running', 'waiting_approval', 'completed', 'failed')
            ),
            user_message TEXT NOT NULL,
            standalone_question TEXT,
            assistant_answer TEXT,
            intent TEXT,
            verified_order_ids_json JSONB NOT NULL,
            cited_document_ids_json JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE (conversation_id, sequence_number),
            UNIQUE (conversation_id, idempotency_key),
            CHECK (updated_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent
        ON conversation_turns (conversation_id, sequence_number DESC)
        """
    )


def downgrade() -> None:
    """禁止自动删除用户会话和工作流关联记录。"""

    raise RuntimeError("会话迁移禁止自动 downgrade；请使用受控备份恢复流程")
