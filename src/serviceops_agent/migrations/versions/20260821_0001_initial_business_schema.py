"""创建退货、事务 Outbox 与只追加审批审计三类业务表。

Revision ID: 20260821_0001
Revises: None
Create Date: 2026-08-21

首版使用 ``IF NOT EXISTS``，可以安全接管第16步已经由应用启动代码创建的本地表；迁移成功后
Alembic 会写入版本号，后续结构变化必须增加新 revision，不能继续修改本文件冒充历史未变化。
"""

# Sequence 为 Alembic 分支标签和依赖字段提供严格类型。
from collections.abc import Sequence

# op 是版本脚本唯一允许执行 DDL 的 Alembic 操作入口。
from alembic import op

# 当前 revision 是数据库结构版本的稳定主键。
revision: str = "20260821_0001"

# None 表示这是 ServiceOps 业务库的第一版迁移。
down_revision: str | None = None

# 当前版本没有并行分支。
branch_labels: str | Sequence[str] | None = None

# 当前版本不依赖其他迁移树。
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """幂等创建第16步已经验证过的业务表、索引和只追加触发器。"""

    # PostgreSQL 事务级建议锁防止两个迁移任务同时执行首版 DDL。
    op.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('serviceops-business-migrations', 0))"
    )
    # 幂等键是主键，多个 API 实例最多创建一条相同业务请求。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS return_requests (
            idempotency_key TEXT PRIMARY KEY,
            return_request_id TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status = 'submitted'),
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    # Outbox 与退货申请处于同一业务事务，避免业务成功但完成审计事实丢失。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS return_outbox_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL CHECK (event_type = 'return_request_committed'),
            aggregate_type TEXT NOT NULL CHECK (aggregate_type = 'return_request'),
            aggregate_id TEXT NOT NULL,
            payload_json JSONB NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'dead_letter')),
            attempts INTEGER NOT NULL CHECK (attempts >= 0),
            next_attempt_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            processed_at TIMESTAMPTZ,
            last_error_code TEXT
        )
        """
    )
    # 协调器按状态、到期时间和创建顺序扫描，索引避免数据增长后全表读取。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_return_outbox_pending
        ON return_outbox_events (status, next_attempt_at, created_at)
        """
    )
    # 线程事件类型和链位置双重唯一，防止同一审批产生重复决定或哈希链分叉。
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_audit_events (
            audit_event_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            request_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            token_jti TEXT NOT NULL,
            approved BOOLEAN NOT NULL,
            order_id TEXT NOT NULL,
            proposal_digest TEXT NOT NULL,
            comment_digest TEXT NOT NULL,
            return_request_id TEXT,
            chain_position INTEGER NOT NULL CHECK (chain_position >= 1),
            created_at TIMESTAMPTZ NOT NULL,
            previous_event_hash TEXT NOT NULL,
            event_hash TEXT NOT NULL UNIQUE,
            UNIQUE (thread_id, event_type),
            UNIQUE (thread_id, chain_position)
        )
        """
    )
    # 审计接口按线程和位置读取整条链。
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_approval_audit_thread_position
        ON approval_audit_events (thread_id, chain_position)
        """
    )
    # 触发器函数统一拒绝普通 UPDATE/DELETE，修正必须追加新事件表达。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION serviceops_reject_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'approval audit events are append-only';
        END;
        $$
        """
    )
    # DROP/CREATE 处于同一 PostgreSQL DDL 事务，其他连接不会看到无触发器的中间状态。
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_approval_audit_update ON approval_audit_events"
    )
    # UPDATE 在实际修改前被阻止。
    op.execute(
        """
        CREATE TRIGGER prevent_approval_audit_update
        BEFORE UPDATE ON approval_audit_events
        FOR EACH ROW EXECUTE FUNCTION serviceops_reject_audit_mutation()
        """
    )
    # DELETE 使用独立触发器名称，便于数据库审计确认两种保护都存在。
    op.execute(
        "DROP TRIGGER IF EXISTS prevent_approval_audit_delete ON approval_audit_events"
    )
    # DELETE 在实际删除前被阻止。
    op.execute(
        """
        CREATE TRIGGER prevent_approval_audit_delete
        BEFORE DELETE ON approval_audit_events
        FOR EACH ROW EXECUTE FUNCTION serviceops_reject_audit_mutation()
        """
    )


def downgrade() -> None:
    """首版生产业务表禁止自动降级删除，避免误操作擦除退货和审计证据。"""

    # 自动 DROP 会让一次错误命令不可逆删除业务事实，必须走单独备份与审批流程。
    raise RuntimeError("首版业务迁移禁止自动 downgrade；请使用受控备份恢复流程")
