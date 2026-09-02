"""增加用户反馈、失败问题池和知识候选表。

Revision ID: 20260902_0004
Revises: 20260829_0003
Create Date: 2026-09-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0004"
down_revision: str | None = "20260829_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建反馈问题池及状态扫描索引。"""

    op.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('serviceops-business-migrations', 0))"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_items (
            feedback_id UUID PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            conversation_id UUID NOT NULL
                REFERENCES conversations(conversation_id) ON DELETE CASCADE,
            turn_id UUID NOT NULL
                REFERENCES conversation_turns(turn_id) ON DELETE CASCADE,
            owner_user_id TEXT NOT NULL,
            signal TEXT NOT NULL CHECK (signal IN ('helpful','unhelpful','auto_handoff')),
            reason TEXT CHECK (
                reason IS NULL OR reason IN (
                    'incorrect','missing_information','bad_citation','not_relevant','other'
                )
            ),
            status TEXT NOT NULL CHECK (
                status IN ('open','triaged','knowledge_candidate','dismissed')
            ),
            category TEXT CHECK (
                category IS NULL OR category IN (
                    'knowledge_gap','retrieval_failure','generation_failure',
                    'workflow_failure','not_actionable'
                )
            ),
            question TEXT NOT NULL,
            answer TEXT,
            intent TEXT,
            cited_document_ids_json JSONB NOT NULL,
            reviewer_id TEXT,
            proposed_title TEXT,
            proposed_answer TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            reviewed_at TIMESTAMPTZ,
            UNIQUE (owner_user_id, idempotency_key),
            CHECK (updated_at >= created_at),
            CHECK (
                (status = 'open' AND category IS NULL
                    AND reviewer_id IS NULL AND reviewed_at IS NULL)
                OR
                (status <> 'open' AND category IS NOT NULL AND reviewer_id IS NOT NULL
                    AND reviewed_at IS NOT NULL)
            ),
            CHECK (
                (status = 'knowledge_candidate' AND category = 'knowledge_gap'
                    AND proposed_title IS NOT NULL AND proposed_answer IS NOT NULL)
                OR
                (status <> 'knowledge_candidate' AND proposed_title IS NULL
                    AND proposed_answer IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_status_created
        ON feedback_items (status, created_at)
        """
    )


def downgrade() -> None:
    """禁止自动删除反馈和人工审核证据。"""

    raise RuntimeError("反馈飞轮迁移禁止自动 downgrade；请使用受控备份恢复流程")
