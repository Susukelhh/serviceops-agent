"""可选的真实 PostgreSQL 集成测试；只在提供专用测试 DSN 时运行。

本测试不会连接开发者未明确提供的数据库。CI 或本地临时测试库可设置
``SERVICEOPS_TEST_POSTGRES_DSN`` 后运行本文件；每次使用唯一线程和幂等键，
避免与并行任务发生主键冲突。
"""

# asyncio.gather/to_thread 让两个独立运行时在不同线程中真正竞争同一数据库幂等键。
import asyncio

# os 只读取显式测试连接地址开关；没有地址时 pytest 会安全跳过本文件。
import os

# uuid4 为共享测试数据库中的线程和业务键生成唯一命名空间。
from uuid import uuid4

# pytest 提供按环境跳过标记，避免普通单元测试误连任何真实数据库。
import pytest

# Alembic command 在创建运行时之前把临时数据库升级到最新业务结构版本。
from alembic import command

# LangGraph Command.resume 用于在第二套运行时恢复第一套运行时留下的 interrupt。
from langgraph.types import Command

# SecretStr 防止测试 DSN 在 Settings repr 或失败输出中显示密码。
from pydantic import SecretStr

# Settings 选择 PostgreSQL 后端并控制测试连接池上限。
from serviceops_agent.config.settings import Settings

# OutboxStatus 验证带 thread_id 的 PostgreSQL JSONB 过滤和状态推进。
from serviceops_agent.domain.outbox import OutboxStatus

# 流程枚举验证跨运行时恢复后的退货完成状态。
from serviceops_agent.domain.returns import ReturnWorkflowStatus

# build_alembic_config 复用容器迁移入口，避免测试维护第二套建表 SQL。
from serviceops_agent.infrastructure.migrate import build_alembic_config

# create_agent_runtime 同时重建官方 PostgreSQL Checkpointer 与业务连接池。
from serviceops_agent.infrastructure.runtime import create_agent_runtime

# 只读取专门命名的测试 DSN；不复用应用 .env 中的生产/开发连接地址。
TEST_POSTGRES_DSN = os.getenv("SERVICEOPS_TEST_POSTGRES_DSN")

# 未显式提供专用测试库时整个文件显示 skipped，而不是尝试猜测 localhost 密码。
pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="需要显式设置 SERVICEOPS_TEST_POSTGRES_DSN 才运行真实 PostgreSQL 测试",
)


async def test_postgres_runtime_shares_checkpoint_business_and_outbox_across_restarts() -> None:
    """两套运行时应共享暂停进度、业务记录和按线程筛选的 Outbox 事件。"""

    # skipif 已保证执行本测试时字符串存在；assert 同时帮助静态类型收窄。
    assert TEST_POSTGRES_DSN is not None
    # 每次测试使用唯一后缀，允许 CI 并行或重复运行而不冲突。
    unique_suffix = uuid4().hex
    # LangGraph thread_id 使用标准 UUID，保持与生产审批 API 路径一致。
    thread_id = str(uuid4())
    # 三次运行时都复用同一 Checkpointer 主键。
    graph_config = {"configurable": {"thread_id": thread_id}}
    # 显式选择 postgres 并把连接地址包装为 SecretStr。
    settings = Settings(
        persistence_backend="postgres",
        postgres_dsn=SecretStr(TEST_POSTGRES_DSN),
        postgres_pool_min_size=1,
        postgres_pool_max_size=3,
        telemetry_enabled=False,
    )

    # Arrange：像 Compose 的 migrate 服务一样，先执行可重复的 upgrade head。
    # 这一步证明 API 运行时不再负责业务 DDL，同时允许测试复用已有数据库。
    command.upgrade(
        build_alembic_config(postgres_dsn=SecretStr(TEST_POSTGRES_DSN)),
        "head",
    )

    # 第一套运行时模拟 API 进程 A。
    async with create_agent_runtime(settings) as first_runtime:
        # 先记录共享测试库已有数量，断言使用增量而不是假设空库。
        initial_return_count = first_runtime.return_request_repository.count()
        # 提交合格请求，图必须暂停在人工审批前。
        paused = await first_runtime.service_graph.ainvoke(
            {
                "request_id": f"postgres-test-request-{unique_suffix}",
                "user_id": "user-001",
                "user_message": "为订单 SO100002 申请退货，原因：PostgreSQL 集成测试",
                "idempotency_key": f"postgres-test-{unique_suffix}",
                "events": ["test:postgres_first_runtime_started"],
            },
            config=graph_config,
        )
        # 审批前不创建退货业务记录。
        assert paused["__interrupt__"]
        assert first_runtime.return_request_repository.count() == initial_return_count

    # 第二套运行时使用全新连接池和 Saver，模拟 API 容器重建。
    async with create_agent_runtime(settings) as second_runtime:
        # 新 Saver 必须从 PostgreSQL 找回原 interrupt，而不是重新从 START 执行。
        snapshot = await second_runtime.service_graph.aget_state(graph_config)
        assert "request_return_approval" in snapshot.next
        assert snapshot.interrupts
        # 注入 thread_id/token_jti，使批准写事务同时创建真实 Outbox 事件。
        completed = await second_runtime.service_graph.ainvoke(
            Command(
                resume={
                    "approved": True,
                    "reviewer_id": "postgres-integration-reviewer",
                    "comment": "跨运行时恢复并批准",
                    "thread_id": thread_id,
                    "token_jti": f"postgres-token-{unique_suffix}",
                }
            ),
            config=graph_config,
        )
        # 图恢复后完成真实业务写入。
        assert completed["return_workflow_status"] == ReturnWorkflowStatus.COMPLETED
        assert completed["return_request_id"].startswith("RR-")
        assert second_runtime.return_request_repository.count() == initial_return_count + 1
        # 该调用覆盖曾在真实 Docker 演练中暴露的“可选 text 参数类型推断”回归点。
        pending_events = second_runtime.return_outbox_repository.list_pending(
            thread_id=thread_id,
            limit=1,
        )
        # 当前线程应恰好取得自己的待处理事件，不会读取其他并行线程。
        assert len(pending_events) == 1
        assert pending_events[0].payload.thread_id == thread_id
        # 状态推进使用 PostgreSQL 行锁并返回最新强类型快照。
        processed_event = second_runtime.return_outbox_repository.mark_processed(
            pending_events[0].event_id
        )
        assert processed_event.status == OutboxStatus.PROCESSED

    # 第三套运行时再次证明终态和业务记录不属于上一个 Python 进程内存。
    async with create_agent_runtime(settings) as third_runtime:
        # 已完成线程不再有 next 或 interrupt。
        final_snapshot = await third_runtime.service_graph.aget_state(graph_config)
        assert final_snapshot.next == ()
        assert final_snapshot.interrupts == ()
        assert final_snapshot.values["return_request_id"] == completed["return_request_id"]
        # 业务记录增量在第二套连接池关闭后仍然存在。
        assert third_runtime.return_request_repository.count() == initial_return_count + 1
        # Outbox 已处理状态同样跨连接池重建保留。
        persisted_event = third_runtime.return_outbox_repository.get_outbox_event(
            processed_event.event_id
        )
        assert persisted_event is not None
        assert persisted_event.status == OutboxStatus.PROCESSED


async def test_postgres_two_runtimes_serialize_same_idempotency_key() -> None:
    """两个 API 运行时同时提交相同业务请求时，数据库只能创建一条记录。"""

    # skipif 已保证真实执行时 DSN 存在；assert 同时收窄 Optional 类型。
    assert TEST_POSTGRES_DSN is not None
    # 每次使用全新幂等键，允许共享 CI 数据库重复运行本测试。
    unique_suffix = uuid4().hex
    # 两个运行时采用相同配置，但会各自创建独立 psycopg 连接池。
    settings = Settings(
        persistence_backend="postgres",
        postgres_dsn=SecretStr(TEST_POSTGRES_DSN),
        postgres_pool_min_size=1,
        postgres_pool_max_size=3,
        telemetry_enabled=False,
    )
    # 与真实 Compose 一致，业务 DDL 必须由 Alembic 在 API 运行时之前完成。
    command.upgrade(
        build_alembic_config(postgres_dsn=SecretStr(TEST_POSTGRES_DSN)),
        "head",
    )

    # 同时保持 A、B 两套运行时存活，模拟两个容器各自持有独立连接池。
    async with (
        create_agent_runtime(settings) as runtime_a,
        create_agent_runtime(settings) as runtime_b,
    ):
        # 记录竞争写入前总数，不假设共享测试数据库为空。
        initial_count = runtime_a.return_request_repository.count()
        # 两边必须提交完全相同的可信业务负载，才属于合法幂等重放。
        common_arguments = {
            "user_id": "user-001",
            "order_id": "SO100002",
            "reason": "PostgreSQL 双实例并发幂等测试",
            "idempotency_key": f"postgres-concurrent-{unique_suffix}",
        }
        # 同步仓储调用放入两个工作线程，使数据库建议锁真实处理并发竞争。
        result_a, result_b = await asyncio.gather(
            asyncio.to_thread(
                runtime_a.return_request_repository.create_or_get,
                **common_arguments,
            ),
            asyncio.to_thread(
                runtime_b.return_request_repository.create_or_get,
                **common_arguments,
            ),
        )
        # 两个调用必须返回完全相同的稳定 RR 编号。
        assert result_a[0].return_request_id == result_b[0].return_request_id
        # 恰好一边首次创建，另一边在锁释放后读取到已有记录并标记为 replay。
        assert sorted((result_a[1], result_b[1])) == [False, True]
        # 无论从 A 还是 B 读取，总数都只能增加一，证明唯一约束没有被突破。
        assert runtime_a.return_request_repository.count() == initial_count + 1
        assert runtime_b.return_request_repository.count() == initial_count + 1
