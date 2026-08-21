"""第七步示例：观察 LangGraph interrupt、人工恢复、拒绝和跨线程幂等。

运行方式：

    uv run python examples/07_human_approval_return.py

示例在导入项目模块前强制使用本地确定性依赖，不调用千问，也不会产生费用。
"""

# os 用当前进程环境变量覆盖本机 `.env`，保证示例结果稳定且零外部请求。
import os

# 分类器固定使用本地关键词基线。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# 订单规划器固定为确定性实现；本例虽不进入订单查询循环，仍避免导入时读取真实开关。
os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "deterministic"
# FAQ 检索固定使用本地 Hash Embedding。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# FAQ 回答固定使用确定性摘录。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# 默认知识索引只存进程内存，不写本地磁盘。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"

# asyncio 负责执行 LangGraph 的异步 ainvoke 接口。
import asyncio

# pprint 让审批负载和事件列表在 PyCharm 控制台中更容易阅读。
from pprint import pprint

# InMemorySaver 保存 interrupt 前状态，供后续使用相同 thread_id 恢复。
from langgraph.checkpoint.memory import InMemorySaver

# Command.resume 把审批人决定交回暂停节点。
from langgraph.types import Command

# 图工厂允许显式注入本示例独享的 Checkpointer 和退货写仓库。
from serviceops_agent.graph.builder import build_service_graph

# 默认订单仓库中 user-001 的 SO100002 已签收，可以进入退货审批。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 进程内退货仓库提供 count，便于直接证明每条路径是否产生写入。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
)


def build_initial_state(
    *,
    request_id: str,
    idempotency_key: str,
    reason: str,
) -> dict[str, object]:
    """构造一条可信 API 初始状态，审批恢复值不能覆盖这些字段。"""

    # 返回图入口需要的最小状态。
    return {
        # request_id 用于关联一次原始请求的事件。
        "request_id": request_id,
        # user_id 在真实系统中应来自 JWT；本例由服务端状态固定注入。
        "user_id": "user-001",
        # 原始文本同时包含明确写动作、本人订单号和带标签原因。
        "user_message": f"为订单 SO100002 申请退货，原因：{reason}",
        # 幂等键在跨线程重试时保持稳定，防止生成第二条业务记录。
        "idempotency_key": idempotency_key,
        # 入口事件会被 State Reducer 与后续节点事件依次累加。
        "events": ["example:request_received"],
    }


def build_thread_config(thread_id: str) -> dict[str, dict[str, str]]:
    """为 Checkpointer 构造包含稳定 thread_id 的配置。"""

    # 恢复时必须原样复用该配置，换一个 ID 会变成另一个空线程。
    return {"configurable": {"thread_id": thread_id}}


async def start_approval(
    graph: object,
    *,
    thread_id: str,
    request_id: str,
    idempotency_key: str,
    reason: str,
) -> tuple[dict[str, object], dict[str, dict[str, str]]]:
    """启动一条退货流程，并返回暂停结果和后续恢复配置。"""

    # 当前线程的启动和恢复会共享这份配置。
    config = build_thread_config(thread_id)
    # graph 在运行时是 CompiledStateGraph；object 注解只让示例避免展示复杂泛型。
    paused = await graph.ainvoke(  # type: ignore[attr-defined]
        # 初始状态由服务端构造，审批端不会再次提交这些业务字段。
        build_initial_state(
            request_id=request_id,
            idempotency_key=idempotency_key,
            reason=reason,
        ),
        # 首次调用也必须提供 thread_id，Checkpointer 才知道状态保存位置。
        config=config,
    )
    # 返回暂停状态和完全相同的恢复配置。
    return paused, config


async def main() -> None:
    """依次演示批准、拒绝和跨线程幂等重试。"""

    # 创建本示例独享的退货仓库，初始记录数为零。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    # 编译启用 Checkpointer 的图；没有 Saver 就无法使用 interrupt 恢复。
    graph = build_service_graph(
        # 所有批准线程共享该仓库，才能演示跨线程业务幂等。
        return_request_repository=repository,
        # InMemorySaver 只适合本地学习和单进程测试。
        checkpointer=InMemorySaver(),
    )

    # 第一条线程使用将被后续重试复用的稳定业务幂等键。
    approved_paused, approved_config = await start_approval(
        graph,
        thread_id="example-return-approved",
        request_id="example-request-approved",
        idempotency_key="example-return-idempotent-001",
        reason="商品尺寸不合适",
    )
    # 读取框架产生的唯一中断对象。
    approval_interrupt = approved_paused["__interrupt__"][0]

    # 场景标题。
    print("=== 场景一：审批前 interrupt，仓库零写入 ===")
    # 打印允许审批人查看的最小负载。
    pprint(approval_interrupt.value)
    # 此时 execute_return_request 节点尚未执行，预期为 0。
    print(f"审批前仓库记录数：{repository.count()}")

    # 使用同一 thread_id 恢复，并提交明确批准决定。
    approved_result = await graph.ainvoke(
        Command(
            # resume 只包含人工决定，不能覆盖 user_id、order_id 或幂等键。
            resume={
                "approved": True,
                "reviewer_id": "reviewer-example-001",
                "comment": "订单与原因核验通过",
            }
        ),
        config=approved_config,
    )
    # 打印批准恢复结果。
    print("\n=== 场景二：批准后才执行写工具 ===")
    # 申请编号来自幂等写仓库。
    print(f"退货申请编号：{approved_result['return_request_id']}")
    # 预期从 0 变为 1。
    print(f"批准后仓库记录数：{repository.count()}")
    # 事件显示 approved → created 的实际控制流。
    print("执行轨迹：")
    pprint(approved_result["events"])

    # 启动另一条使用不同幂等键的线程，用于演示拒绝路径。
    _, rejected_config = await start_approval(
        graph,
        thread_id="example-return-rejected",
        request_id="example-request-rejected",
        idempotency_key="example-return-rejected-001",
        reason="暂时不需要这个商品",
    )
    # 记录拒绝前已有的唯一记录数。
    count_before_rejection = repository.count()
    # 恢复线程并明确拒绝。
    rejected_result = await graph.ainvoke(
        Command(
            resume={
                "approved": False,
                "reviewer_id": "reviewer-example-002",
                "comment": "当前不批准创建申请",
            }
        ),
        config=rejected_config,
    )
    # 打印拒绝场景。
    print("\n=== 场景三：拒绝后零新增 ===")
    # 用户可见回答明确说明未创建。
    print(rejected_result["answer"])
    # 拒绝前后记录数应完全相同。
    print(f"拒绝前记录数：{count_before_rejection}")
    print(f"拒绝后记录数：{repository.count()}")

    # 新线程故意复用第一条已批准请求的相同幂等键和相同业务负载。
    _, replay_config = await start_approval(
        graph,
        thread_id="example-return-idempotent-retry",
        request_id="example-request-idempotent-retry",
        idempotency_key="example-return-idempotent-001",
        reason="商品尺寸不合适",
    )
    # 第二个线程也获批准，用于模拟客户端没有收到第一次响应而重新发起。
    replay_result = await graph.ainvoke(
        Command(
            resume={
                "approved": True,
                "reviewer_id": "reviewer-example-003",
                "comment": "批准客户端幂等重试",
            }
        ),
        config=replay_config,
    )
    # 打印跨线程幂等场景。
    print("\n=== 场景四：跨线程相同幂等键不重复创建 ===")
    # 新线程返回与第一条完全相同的申请编号。
    print(f"首次编号：{approved_result['return_request_id']}")
    print(f"重试编号：{replay_result['return_request_id']}")
    # 仓库仍只有第一条批准记录；拒绝线程和重试线程都没有新增。
    print(f"最终仓库记录数：{repository.count()}")
    # 特定事件证明这是幂等重放而不是第二次写入。
    print("是否命中幂等重放事件：")
    print("graph:return_request_idempotent_replay" in replay_result["events"])


# 只有直接运行本文件时才创建和关闭事件循环。
if __name__ == "__main__":
    # asyncio.run 是普通 Python 脚本调用异步 LangGraph 的标准入口。
    asyncio.run(main())
