"""第八步示例：关闭全部资源后，用 SQLite 恢复原审批线程和业务记录。

运行方式：

    uv run python examples/08_sqlite_restart_recovery.py

示例使用临时 SQLite 文件模拟三次服务启动，退出后自动清理，不污染 data/runtime。
"""

# os 在导入项目模块前固定所有模型相关后端，保证零网络、零 Token。
import os

# 分类使用本地关键词规则。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# 订单 Agent 使用确定性规划器。
os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "deterministic"
# FAQ 使用本地 Hash Embedding。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# FAQ 回答使用确定性摘录。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# Qdrant 只保存在当前进程内存。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"

# asyncio 运行异步 SQLite Checkpointer 和 LangGraph。
import asyncio

# Path 构造跨平台临时数据库路径。
from pathlib import Path

# TemporaryDirectory 创建示例独享目录，并在全部连接关闭后自动删除。
from tempfile import TemporaryDirectory

# Command.resume 在第二次运行时提交批准决定。
from langgraph.types import Command

# Settings 显式选择 SQLite 和两个数据库文件。
from serviceops_agent.config.settings import Settings

# create_agent_runtime 管理 Saver 连接、业务仓库与图的共同生命周期。
from serviceops_agent.infrastructure.runtime import create_agent_runtime


def build_config() -> dict[str, dict[str, str]]:
    """返回三次运行必须完全复用的稳定线程配置。"""

    # thread_id 是 Checkpoint 查询主键，不是退货业务幂等键。
    return {"configurable": {"thread_id": "example-sqlite-restart-thread"}}


async def main() -> None:
    """依次模拟启动 A、关闭、启动 B 恢复、再关闭和启动 C 验证。"""

    # 临时目录在 with 结束后删除，适合反复从 PyCharm 运行。
    with TemporaryDirectory(prefix="serviceops-step08-") as temporary_directory:
        # 转换为 Path，便于拼接两个独立数据库文件。
        runtime_directory = Path(temporary_directory)
        # 构造 SQLite 本地运行配置。
        settings = Settings(
            # 明确选择磁盘后端。
            persistence_backend="sqlite",
            # Checkpoint 保存 LangGraph State、执行位置和 pending writes。
            checkpoint_database_path=str(runtime_directory / "checkpoints.sqlite3"),
            # 业务库保存真实退货申请，不与工作流快照混为一张表。
            business_database_path=str(runtime_directory / "serviceops.sqlite3"),
        )
        # 三次运行复用同一个 thread_id。
        config = build_config()

        # 第一次上下文代表服务进程 A 启动。
        async with create_agent_runtime(settings) as first_runtime:
            # 发起合格退货请求，图会在 interrupt 节点暂停。
            paused = await first_runtime.service_graph.ainvoke(
                {
                    # 原始请求链路标识。
                    "request_id": "example-sqlite-request-001",
                    # 可信用户身份。
                    "user_id": "user-001",
                    # 本人已签收订单和明确退货原因。
                    "user_message": "为订单 SO100002 申请退货，原因：商品尺寸不合适",
                    # 业务幂等键独立于 thread_id。
                    "idempotency_key": "example-sqlite-business-key-001",
                    # 初始事件会写入磁盘 Checkpoint。
                    "events": ["example:first_runtime_started"],
                },
                # 首次调用就必须提供线程配置。
                config=config,
            )
            # 打印第一次运行结果。
            print("=== 运行 A：图已暂停，尚未写业务库 ===")
            # __interrupt__ 非空证明状态已交给 Checkpointer。
            print(f"中断数量：{len(paused['__interrupt__'])}")
            # 审批前必须为零。
            print(f"退货记录数：{first_runtime.return_request_repository.count()}")
        # 离开 async with 后第一套 Saver 连接已关闭，模拟进程 A 完全退出。

        # 显示两个数据库文件已经真正写到磁盘。
        print("\n=== 运行 A 已关闭：SQLite 文件仍存在 ===")
        print(f"Checkpoint 文件：{Path(settings.checkpoint_database_path).exists()}")
        print(f"业务库文件：{Path(settings.business_database_path).exists()}")

        # 第二次上下文创建全新的连接、仓库对象和编译图，模拟服务进程 B。
        async with create_agent_runtime(settings) as second_runtime:
            # 从磁盘读取原 thread_id 的最新状态。
            snapshot = await second_runtime.service_graph.aget_state(config)
            # 打印恢复位置。
            print("\n=== 运行 B：从磁盘找到原审批位置 ===")
            print(f"下一节点：{snapshot.next}")
            print(f"待处理 interrupt 数量：{len(snapshot.interrupts)}")

            # 在新运行时中恢复并批准原请求。
            completed = await second_runtime.service_graph.ainvoke(
                Command(
                    # resume 只提交决定，原订单、身份和幂等键来自磁盘 State。
                    resume={
                        "approved": True,
                        "reviewer_id": "reviewer-step08-001",
                        "comment": "重启恢复后批准",
                    }
                ),
                # 必须继续使用相同 thread_id。
                config=config,
            )
            # 保存编号供第三次运行对比。
            return_request_id = completed["return_request_id"]
            # 打印批准后的持久化结果。
            print(f"退货申请编号：{return_request_id}")
            print(f"退货记录数：{second_runtime.return_request_repository.count()}")
        # 第二套资源也已完全关闭。

        # 第三次上下文模拟服务再次重启。
        async with create_agent_runtime(settings) as third_runtime:
            # 读取同一线程的最新终态。
            final_snapshot = await third_runtime.service_graph.aget_state(config)
            # 打印最终验证。
            print("\n=== 运行 C：工作流终态和业务记录都仍存在 ===")
            print(f"下一节点：{final_snapshot.next}")
            print(
                "Checkpoint 中的申请编号："
                f"{final_snapshot.values['return_request_id']}"
            )
            print(f"业务库记录数：{third_runtime.return_request_repository.count()}")
            # 两边编号相等证明工作流状态与业务效果一致。
            print(
                "编号是否一致："
                f"{final_snapshot.values['return_request_id'] == return_request_id}"
            )
        # 三套异步连接全部关闭后 TemporaryDirectory 才会安全删除文件。


# 直接运行脚本时创建并关闭事件循环。
if __name__ == "__main__":
    # asyncio.run 是本地脚本调用异步运行时的标准入口。
    asyncio.run(main())
