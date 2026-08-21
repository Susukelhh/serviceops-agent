"""第十二步示例：事务 Outbox、进程重启补偿与幂等重复协调。

在 PyCharm 中直接运行本文件，或在项目根目录执行：

    uv run python examples/12_transactional_outbox_recovery.py

本示例完全离线，不调用千问 API，也不会修改项目正式的 data/runtime 数据库。
"""

# Path 把临时目录转换为 SQLite 仓库接受的明确路径。
from pathlib import Path

# TemporaryDirectory 创建一次演示独享目录，脚本结束后自动回收临时数据库。
from tempfile import TemporaryDirectory

# 协调器负责把业务 Outbox 至少一次投递到审批审计哈希链。
from serviceops_agent.application.outbox_reconciler import ReturnOutboxReconciler

# 审计模型和摘要函数帮助构造与真实 API 相同的最小证据。
from serviceops_agent.domain.audit import (
    ApprovalAuditDraft,
    ApprovalAuditEventType,
    build_comment_digest,
    build_proposal_digest,
)

# Outbox 元数据只保存可信身份、关联标识和摘要，不保存完整 Token/备注/原因。
from serviceops_agent.domain.outbox import OutboxStatus, ReturnOutboxMetadata

# 退货草案用于演示人工审批前已经固定的业务参数。
from serviceops_agent.domain.returns import ReturnRequestProposal

# 审计与业务仓库指向同一 SQLite 文件中的不同表。
from serviceops_agent.infrastructure.audit_repository import (
    SQLiteApprovalAuditRepository,
)
from serviceops_agent.infrastructure.order_repository import default_order_repository
from serviceops_agent.infrastructure.return_repository import (
    SQLiteReturnRequestRepository,
)


def main() -> None:
    """依次演示原子提交、进程退出、重启补偿和幂等空重试。"""

    # 临时目录保证脚本可以反复运行而不与以前的稳定事件 ID 冲突。
    with TemporaryDirectory(prefix="serviceops-step12-") as temporary_directory:
        # 业务记录、Outbox 和审批审计表都位于同一演示 SQLite 文件。
        database_path = Path(temporary_directory) / "step12-outbox-demo.db"
        # 模拟真实 Agent 在 interrupt 前生成并保存到 Checkpoint 的草案。
        proposal = ReturnRequestProposal(
            action="create_return_request",
            order_id="SO100002",
            reason="商品尺寸不合适，需要退货",
            idempotency_key="step12-outbox-demo-key-001",
            risk_level="write",
        )
        # API 会从路径、Checkpoint 和已验签 JWT 构造这些可信字段。
        metadata = ReturnOutboxMetadata(
            thread_id="step12-outbox-thread-001",
            request_id="step12-outbox-request-001",
            actor_id="reviewer-step12-001",
            token_jti="token-jti-step12-001",
            approved=True,
            order_id=proposal.order_id,
            proposal_digest=build_proposal_digest(proposal),
            comment_digest=build_comment_digest("已核验订单，批准创建"),
        )

        # 运行 A：创建第一组仓库对象，模拟服务进程启动。
        first_return_repository = SQLiteReturnRequestRepository(
            database_path=database_path,
            order_repository=default_order_repository,
        )
        first_audit_repository = SQLiteApprovalAuditRepository(
            database_path=database_path
        )
        # 真实 API 会在恢复 LangGraph 前先记录审批决定。
        first_audit_repository.append(
            ApprovalAuditDraft(
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
        )
        # 关键调用：业务记录与 Outbox 事件使用同一个 SQLite 事务提交。
        return_record, _ = first_return_repository.create_or_get(
            user_id="user-001",
            order_id=proposal.order_id,
            reason=proposal.reason,
            idempotency_key=proposal.idempotency_key,
            outbox_metadata=metadata,
        )

        print("=== 运行 A：业务事务提交后、协调器运行前 ===")
        print(f"退货申请编号：{return_record.return_request_id}")
        print(f"业务记录数：{first_return_repository.count()}")
        print(
            "待处理 Outbox 数："
            f"{first_return_repository.count_outbox(OutboxStatus.PENDING)}"
        )
        print(
            "当前审计事件：",
            [
                event.event_type.value
                for event in first_audit_repository.list_for_thread(metadata.thread_id)
            ],
        )
        print("现在模拟进程在投递完成事件前退出。")

        # 运行 B：不复用任何仓库 Python 对象，只复用同一磁盘文件，模拟服务重启。
        restarted_return_repository = SQLiteReturnRequestRepository(
            database_path=database_path,
            order_repository=default_order_repository,
        )
        restarted_audit_repository = SQLiteApprovalAuditRepository(
            database_path=database_path
        )
        # 新协调器扫描上次进程留下的 pending 事件。
        first_reconciliation = ReturnOutboxReconciler(
            outbox_repository=restarted_return_repository,
            audit_repository=restarted_audit_repository,
        ).reconcile()

        print("\n=== 运行 B：重启后补偿 ===")
        print(f"协调结果：{first_reconciliation.model_dump()}")
        print(
            "已处理 Outbox 数："
            f"{restarted_return_repository.count_outbox(OutboxStatus.PROCESSED)}"
        )
        print(
            "完整审计事件：",
            [
                event.event_type.value
                for event in restarted_audit_repository.list_for_thread(
                    metadata.thread_id
                )
            ],
        )
        print(
            "审计哈希链是否有效：",
            restarted_audit_repository.verify_thread_chain(metadata.thread_id),
        )

        # 再运行一次证明 processed 事件不会重复追加审计链。
        second_reconciliation = ReturnOutboxReconciler(
            outbox_repository=restarted_return_repository,
            audit_repository=restarted_audit_repository,
        ).reconcile()
        print("\n=== 同一补偿任务再次执行 ===")
        print(f"协调结果：{second_reconciliation.model_dump()}")
        print(
            "审计事件总数仍为：",
            len(
                restarted_audit_repository.list_for_thread(metadata.thread_id)
            ),
        )


# 只有在 PyCharm 或命令行直接运行本文件时才执行演示。
if __name__ == "__main__":
    main()
