"""SQLite AgentRuntime 在资源完全关闭并重建后的审批恢复集成测试。"""

# Path 标注 pytest 临时目录。
from pathlib import Path

# Command.resume 用于第二次进程启动后恢复第一次启动留下的 interrupt。
from langgraph.types import Command

# Settings 使用临时绝对路径构建独立 SQLite 运行时。
from serviceops_agent.config.settings import Settings

# 审计草稿和事件枚举用于验证业务数据库中的证据链同样跨重启保留。
from serviceops_agent.domain.audit import ApprovalAuditDraft, ApprovalAuditEventType

# 流程枚举验证批准恢复后的最终业务阶段。
from serviceops_agent.domain.returns import ReturnWorkflowStatus

# create_agent_runtime 同时管理 AsyncSqliteSaver、图和磁盘业务仓库。
from serviceops_agent.infrastructure.runtime import create_agent_runtime


def _sqlite_settings(tmp_path: Path) -> Settings:
    """构造不读取开发者 .env 路径的测试专用 SQLite 配置。"""

    # 只覆盖持久化相关字段，其余模型/RAG配置由 conftest 的离线环境变量提供。
    return Settings(
        # 明确选择本地磁盘后端。
        persistence_backend="sqlite",
        # Checkpoint 与业务记录使用两个数据库文件。
        checkpoint_database_path=str(tmp_path / "checkpoints.sqlite3"),
        business_database_path=str(tmp_path / "serviceops.sqlite3"),
    )


def _thread_config() -> dict[str, dict[str, str]]:
    """返回三次运行都复用的稳定 Checkpointer 线程配置。"""

    # 相同 thread_id 是跨进程找到原暂停位置的必要条件。
    return {"configurable": {"thread_id": "persistent-restart-thread-001"}}


async def test_sqlite_runtime_resumes_interrupt_after_full_resource_restart(
    tmp_path: Path,
) -> None:
    """第一运行暂停并关闭后，第二运行应恢复审批，第三运行应看到完成状态。"""

    # Arrange：三个模拟进程启动使用相同绝对数据库路径。
    settings = _sqlite_settings(tmp_path)
    config = _thread_config()

    # 第一次 async with 模拟服务进程 A 的完整 lifespan。
    async with create_agent_runtime(settings) as first_runtime:
        # Act：提交合格退货请求，图应在 interrupt 暂停。
        paused = await first_runtime.service_graph.ainvoke(
            {
                "request_id": "persistent-request-001",
                "user_id": "user-001",
                "user_message": "为订单 SO100002 申请退货，原因：商品尺寸不合适",
                "idempotency_key": "persistent-business-key-001",
                "events": ["test:first_process_started"],
            },
            config=config,
        )
        # Assert：首次运行确实返回框架 interrupt。
        assert paused["__interrupt__"]
        # Assert：审批前磁盘业务库仍然零写入。
        assert first_runtime.return_request_repository.count() == 0
    # 离开上下文后，第一套 aiosqlite 连接和图运行时已经完全关闭。

    # 第二次 async with 创建全新的 Saver、仓库和图，模拟服务进程 B。
    async with create_agent_runtime(settings) as second_runtime:
        # Act：新实例通过相同 thread_id 读取磁盘快照。
        snapshot = await second_runtime.service_graph.aget_state(config)
        # Assert：下一节点仍然是原来的审批 interrupt，而不是从 START 重新执行。
        assert "request_return_approval" in snapshot.next
        assert snapshot.interrupts

        # Act：在新运行时中批准并恢复第一次运行的状态。
        completed = await second_runtime.service_graph.ainvoke(
            Command(
                resume={
                    "approved": True,
                    "reviewer_id": "persistent-reviewer-001",
                    "comment": "跨进程恢复后批准",
                }
            ),
            config=config,
        )
        # Assert：恢复后进入写工具完成态。
        assert completed["return_workflow_status"] == ReturnWorkflowStatus.COMPLETED
        assert completed["return_request_id"].startswith("RR-")
        # Assert：第二运行的磁盘业务仓库恰好新增一行。
        assert second_runtime.return_request_repository.count() == 1
        # 保存编号供第三次启动比较。
        return_request_id = completed["return_request_id"]
        # Act：API 层正常会在恢复前后追加这两条事件；本集成测试直接验证运行时仓库装配。
        second_runtime.approval_audit_repository.append(
            ApprovalAuditDraft(
                # 与 Checkpointer 使用相同线程标识。
                thread_id=config["configurable"]["thread_id"],
                # 第一条表达审批决定已经提交。
                event_type=ApprovalAuditEventType.DECISION_RECORDED,
                # 关联最初请求。
                request_id="persistent-request-001",
                # 审批人来自测试中的可信恢复值。
                actor_id="persistent-reviewer-001",
                # 只保存演示 jti，不保存 Token。
                token_jti="persistent-token-jti-001",
                # 本次决定为批准。
                approved=True,
                # 目标订单来自原草案。
                order_id="SO100002",
                # 固定合法摘要代表经过哈希的草案和备注。
                proposal_digest="a" * 64,
                comment_digest="b" * 64,
            )
        )
        # Act：第二条表达写工具已经完成并绑定真实申请号。
        second_runtime.approval_audit_repository.append(
            ApprovalAuditDraft(
                thread_id=config["configurable"]["thread_id"],
                event_type=ApprovalAuditEventType.WORKFLOW_COMPLETED,
                request_id="persistent-request-001",
                actor_id="persistent-reviewer-001",
                token_jti="persistent-token-jti-001",
                approved=True,
                order_id="SO100002",
                proposal_digest="a" * 64,
                comment_digest="b" * 64,
                # 结果编号必须与图真实返回值一致。
                return_request_id=return_request_id,
            )
        )
        # Assert：第二运行内链完整有效。
        assert second_runtime.approval_audit_repository.verify_thread_chain(
            config["configurable"]["thread_id"]
        )
    # 第二套 Checkpointer 连接也在这里关闭。

    # 第三次运行模拟再次重启，用于验证工作流终态和业务记录都持久存在。
    async with create_agent_runtime(settings) as third_runtime:
        # Act：读取同一线程最新快照。
        completed_snapshot = await third_runtime.service_graph.aget_state(config)
        # Assert：已完成线程没有下一节点或待处理 interrupt。
        assert completed_snapshot.next == ()
        assert completed_snapshot.interrupts == ()
        # Assert：最新 State 保留相同退货申请编号。
        assert completed_snapshot.values["return_request_id"] == return_request_id
        # Assert：独立新仓库实例仍能从磁盘看到唯一记录。
        assert third_runtime.return_request_repository.count() == 1
        # Act：第三运行从同一业务 SQLite 读取上一运行的两条审计事件。
        persisted_audit_events = (
            third_runtime.approval_audit_repository.list_for_thread(
                config["configurable"]["thread_id"]
            )
        )
        # Assert：审计链与业务记录一起跨完整运行时重启保留。
        assert len(persisted_audit_events) == 2
        assert persisted_audit_events[1].return_request_id == return_request_id
        assert third_runtime.approval_audit_repository.verify_thread_chain(
            config["configurable"]["thread_id"]
        )
