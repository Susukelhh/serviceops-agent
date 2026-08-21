"""第六步示例：观察受控 Agent 的规划—工具—观察循环与最大步数停止。

运行方式：

    uv run python examples/06_controlled_tool_loop.py

示例在导入项目模块前强制使用确定性规划器和本地依赖，不会调用千问或产生费用。
"""

# os 用进程环境变量覆盖本机 `.env`，确保该教学示例零外部调用且结果可重复。
import os

# 默认分类使用关键词基线。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# 工具规划使用确定性订单号与观察历史，而不是收费模型。
os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "deterministic"
# FAQ 依赖虽然不会走到，仍固定本地 Hash Embedding 以避免全局图导入时访问网络。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# FAQ 回答器保持确定性摘录模式。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# 全局图导入时建立的 Qdrant 索引只保存在内存中。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"

# asyncio 执行包含异步规划节点的 LangGraph。
import asyncio

# pprint 让事件列表和多订单号列表在 PyCharm 控制台中保持可读。
from pprint import pprint

# build_service_graph 支持为第二个演示构建更小工具预算的独立图。
from serviceops_agent.graph.builder import build_service_graph


async def run_normal_multi_order_loop() -> None:
    """执行两个订单查询，观察工具节点如何通过回边运行两次。"""

    # 默认图最大允许三次工具，足以处理当前两个唯一订单。
    graph = build_service_graph(agent_max_tool_steps=3)
    # ainvoke 运行完整入口分类和订单 Agent 子图。
    result = await graph.ainvoke(
        {
            # 固定请求 ID 便于阅读输出。
            "request_id": "example-controlled-loop-success",
            # 两个示例订单都属于 user-001。
            "user_id": "user-001",
            # 一条请求同时包含两个工具目标，强制产生真实循环。
            "user_message": "请查询订单 SO100001 和 SO100002 的状态",
            # 入口事件会与循环中的所有节点事件累积。
            "events": ["example:started"],
        }
    )

    # 打印成功场景标题。
    print("=== 场景一：两个订单的受控工具循环 ===")
    # 显示确定性汇总答案，两行分别来自两次工具观察。
    print(result["answer"])
    # 显示实际工具调用次数，预期为 2。
    print(f"工具调用次数：{result['tool_call_count']}")
    # 显示按执行顺序记录的订单号。
    print("已查询订单：")
    # pprint 输出 Python 列表。
    pprint(result["queried_order_ids"])
    # 显示循环正常完成原因。
    print(f"停止原因：{result['agent_stop_reason']}")
    # 完整事件中应出现两次 planned_tool_call 和两次 order_tool_executed。
    print("执行轨迹：")
    # pprint 便于逐项观察回边顺序。
    pprint(result["events"])


async def run_max_step_guard() -> None:
    """用一次工具预算处理两个订单，观察第二次调用如何在执行前被阻止。"""

    # 独立图把工具硬预算设为一次。
    limited_graph = build_service_graph(agent_max_tool_steps=1)
    # 问题需要两个调用，因此第一条观察后下一轮规划会触发预算门。
    result = await limited_graph.ainvoke(
        {
            # 固定请求标识。
            "request_id": "example-controlled-loop-max-step",
            # 当前可信身份仍为 user-001。
            "user_id": "user-001",
            # 两个目标超过本图一次工具预算。
            "user_message": "请查询订单 SO100001 和 SO100002 的状态",
            # 单独的入口事件区分两个演示场景。
            "events": ["example:started"],
        }
    )

    # 使用空行分隔两个场景。
    print("\n=== 场景二：最大工具步数安全停止 ===")
    # 最终答案来自人工接管节点，不会返回不完整的部分任务答案。
    print(result["answer"])
    # 只执行了预算允许的第一次工具。
    print(f"工具调用次数：{result['tool_call_count']}")
    # 停止原因应为 max_tool_steps_exceeded。
    print(f"停止原因：{result['agent_stop_reason']}")
    # True 告诉 API 调用方后续应进入人工处理。
    print(f"需要人工：{result['requires_human']}")
    # 打印事件确认第二次工具没有出现 order_tool_executed。
    print("执行轨迹：")
    # pprint 保持列表清晰。
    pprint(result["events"])


async def main() -> None:
    """按顺序运行正常循环与安全停止两个场景。"""

    # 先演示真正的多工具回边。
    await run_normal_multi_order_loop()
    # 再演示循环预算不是提示词建议，而是服务端硬限制。
    await run_max_step_guard()


# 只有直接运行该文件时才创建事件循环。
if __name__ == "__main__":
    # asyncio.run 负责创建和关闭本地事件循环。
    asyncio.run(main())
