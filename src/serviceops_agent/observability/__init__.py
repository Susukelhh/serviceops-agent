"""ServiceOps 的 Trace、Metrics 和关联结构化日志公共入口。"""

# 从实现模块集中导出图节点包装器、运行时配置和业务指标函数。
from serviceops_agent.observability.telemetry import (
    TelemetryRuntime,
    add_current_span_attributes,
    configure_telemetry,
    current_trace_id,
    force_flush_telemetry,
    instrument_graph_node,
    record_agent_execution,
    record_approval_execution,
    record_conversation_shadow_observation,
    start_safe_span,
)

# __all__ 明确稳定公共 API，避免调用方依赖模块内部 Provider/Exporter 细节。
__all__ = [
    "TelemetryRuntime",
    "add_current_span_attributes",
    "configure_telemetry",
    "current_trace_id",
    "force_flush_telemetry",
    "instrument_graph_node",
    "record_agent_execution",
    "record_approval_execution",
    "record_conversation_shadow_observation",
    "start_safe_span",
]
