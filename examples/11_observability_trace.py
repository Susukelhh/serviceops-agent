"""第十一步示例：离线运行 Agent，并观察父子 Trace、Metrics 和关联 JSON 日志。

运行方式：

    uv run python examples/11_observability_trace.py

示例强制使用 mock/本地仓库，不调用千问或任何外部网络；Console Exporter 会打印较多 JSON。
"""

# asyncio 运行异步 LangGraph；logging 生成关联日志；os 固定当前进程离线配置。
import asyncio
import logging
import os

# perf_counter 测量整个图执行耗时。
from time import perf_counter

# 在导入 Settings/Graph 前强制所有模型与存储后端保持本地确定性。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "deterministic"
os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
# 示例明确开启遥测并输出到当前 PyCharm 控制台。
os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "true"
os.environ["SERVICEOPS_TELEMETRY_EXPORTER"] = "console"
# 使用长周期避免示例中途重复导出；结尾会显式 force_flush 一次。
os.environ["SERVICEOPS_OTEL_METRIC_EXPORT_INTERVAL_MS"] = "60000"

# get_settings 读取上面的进程级安全配置。
from serviceops_agent.config.settings import get_settings

# build_service_graph 构建已经统一包装节点 Span 的完整业务图。
from serviceops_agent.graph.builder import build_service_graph

# 遥测函数负责 Provider 配置、根业务 Span、指标记录与最终刷新。
from serviceops_agent.observability.telemetry import (
    configure_telemetry,
    current_trace_id,
    force_flush_telemetry,
    record_agent_execution,
    start_safe_span,
)

# 使用项目命名空间 Logger，configure_telemetry 后会输出单行关联 JSON。
logger = logging.getLogger("serviceops_agent.examples.observability")


async def main() -> None:
    """执行一次多订单查询，并把所有节点关联到同一 Trace。"""

    # 读取安全离线配置。
    settings = get_settings()
    # 初始化 Console Trace/Metrics Exporter 和项目 JSON Logger。
    telemetry_runtime = configure_telemetry(settings)
    # 示例明确开启遥测，因此运行时不应为空。
    if telemetry_runtime is None:
        raise RuntimeError("示例需要 SERVICEOPS_TELEMETRY_ENABLED=true")

    # 构建完整图；分类、规划、工具和响应节点都已由 instrument_graph_node 包装。
    graph = build_service_graph()
    # 使用固定标识方便在 Console Exporter 输出中搜索。
    request_id = "observability-example-request-001"
    thread_id = "observability-example-thread-001"

    # 根业务 Span 模拟真实 HTTP Server Span 下的 serviceops.agent.chat。
    with start_safe_span(
        "serviceops.example.agent_request",
        attributes={
            # Trace 允许请求/线程关联，但不会把这些字段写入 Metrics 标签。
            "serviceops.request.id": request_id,
            "serviceops.thread.id": thread_id,
            "serviceops.operation": "example",
        },
    ):
        # 在根 Span 内读取 Trace ID，后续所有节点应共享这个 ID。
        trace_id = current_trace_id()
        print(f"本次示例 Trace ID：{trace_id}")
        # 记录一条关联日志；不会记录下面的 user_message 正文。
        logger.info(
            "开始执行离线多订单 Agent 示例",
            extra={
                "request_id": request_id,
                "thread_id": thread_id,
                "operation": "example",
            },
        )
        # 开始端到端耗时测量。
        started_at = perf_counter()
        # 执行两次工具调用的确定性 Agent 环。
        result = await graph.ainvoke(
            {
                "request_id": request_id,
                # 身份只进入业务 State，不进入指标标签或日志。
                "user_id": "user-001",
                # 原始文本不会进入遥测；节点 Span 只记录名称、意图和结果。
                "user_message": "查询订单 SO100001 和 SO100002 的状态",
                "events": ["example:observability_started"],
            },
            config={"configurable": {"thread_id": thread_id}},
        )
        # 记录有限 operation/intent/outcome/tool 指标。
        record_agent_execution(
            operation="chat",
            result=result,
            duration_ms=(perf_counter() - started_at) * 1_000,
        )
        # 结束日志与开始日志拥有同一 trace_id，但 span_id 对应当前根业务 Span。
        logger.info(
            "离线多订单 Agent 示例执行完成",
            extra={
                "request_id": request_id,
                "thread_id": thread_id,
                "operation": "example",
                "outcome": "completed",
            },
        )

    # 根 Span 结束后打印易读业务摘要；完整 Span JSON 会在 flush 时输出。
    print(f"识别意图：{result['intent']}")
    print(f"工具调用次数：{result['tool_call_count']}")
    print(f"查询订单：{result['queried_order_ids']}")
    print("正在刷新 Console Trace/Metrics，请继续观察后面的 JSON 输出……")
    # 强制导出 BatchProcessor 中的所有 Span 和当前聚合 Metrics。
    flushed = force_flush_telemetry()
    print(f"遥测刷新是否完成：{flushed}")
    # 提醒学习者执行安全检查。
    print("请搜索 Trace ID，确认父子 Span 相同；输出不应包含用户原文、Token 或 API Key。")


# 只有直接运行示例时才创建事件循环。
if __name__ == "__main__":
    # asyncio.run 负责创建并关闭单次示例事件循环。
    asyncio.run(main())
