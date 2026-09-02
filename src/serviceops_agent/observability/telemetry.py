"""OpenTelemetry Trace/Metrics 与可关联 JSON 日志的安全实现。

设计边界：

- Trace 可以携带 request_id/thread_id 等单次请求标识，但不携带用户原文、Token 或模型响应；
- Metrics 只能使用有限枚举属性，禁止 user_id/request_id/thread_id 等高基数标签；
- 日志只输出调用方显式提供的安全字段，并把换行归一化，降低日志注入风险；
- 异常只记录类型，不让 SDK 自动把可能含敏感正文的异常消息写进 Span。
"""

# json 生成单行结构化日志；logging 复用项目已有标准库日志调用。
import json
import logging
import re

# sys.stdout 让本地 PyCharm/Uvicorn 控制台可以直接观察 JSON 日志。
import sys

# Awaitable/Callable/Iterator 标注图节点包装器和上下文管理器。
from collections.abc import Awaitable, Callable, Iterator

# contextmanager 让调用方用 with 创建安全 Span；dataclass 保存可 flush 的 Provider。
from contextlib import contextmanager
from dataclasses import dataclass

# perf_counter 提供不受系统时钟调整影响的耗时测量；UTC datetime 生成日志时间。
from datetime import UTC, datetime

# Lock 保护进程级 OpenTelemetry Provider 只初始化一次。
from threading import Lock
from time import perf_counter

# Any/cast 处理 LangGraph 节点同步或异步返回值，同时保持严格 Mypy。
from typing import Any, cast

# RunnableLambda 把包装后的异步函数变成 StateGraph 类型系统可识别的 Runnable。
from langchain_core.runnables import RunnableLambda

# GraphInterrupt 是 LangGraph 正常人工暂停控制流，不能误标记为系统异常。
from langgraph.errors import GraphInterrupt

# OpenTelemetry API 提供全局代理 Tracer/Meter 与当前 Span 上下文。
from opentelemetry import metrics, trace

# OTLP/HTTP Exporter 把 Trace 和 Metrics 发送给本地或集群 Collector。
from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# MeterProvider 和周期 Reader 管理稳定 Metrics SDK 的聚合与导出。
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricExporter,
    PeriodicExportingMetricReader,
)

# Resource 为所有信号附加稳定服务/环境元数据，不使用请求级字段。
from opentelemetry.sdk.resources import Resource

# ParentBased/TraceIdRatioBased 让上游采样决定优先，并控制本服务新 Trace 的比例。
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

# Span/Status 用于手工添加安全属性和失败状态；不自动记录异常正文。
from opentelemetry.trace import Span, Status, StatusCode

# Settings 提供导出器、服务名、环境、采样率和导出周期。
from serviceops_agent.config.settings import Settings

# ServiceState 是所有 LangGraph 节点共享的强类型状态。
from serviceops_agent.graph.state import ServiceState

# 模块级代理在 Provider 配置前也可以安全创建；配置后会自动委托给真实 SDK。
tracer = trace.get_tracer("serviceops_agent", "0.1.0")
# Meter 使用稳定 instrumentation scope，便于后端按库版本聚合。
meter = metrics.get_meter("serviceops_agent", "0.1.0")

# 节点耗时直方图只使用有限 node/outcome 属性。
graph_node_duration = meter.create_histogram(
    "serviceops.graph.node.duration",
    unit="ms",
    description="LangGraph 节点执行耗时",
)
# Agent 请求计数器按 operation/intent/outcome 聚合，不放请求或用户标识。
agent_execution_counter = meter.create_counter(
    "serviceops.agent.executions",
    unit="1",
    description="Agent 对话和审批恢复执行次数",
)
# Agent 总耗时直方图帮助计算 P50/P95/P99。
agent_execution_duration = meter.create_histogram(
    "serviceops.agent.execution.duration",
    unit="ms",
    description="Agent 图执行端到端耗时",
)
# 工具调用计数只按有限工具名和结果聚合。
tool_call_counter = meter.create_counter(
    "serviceops.agent.tool.calls",
    unit="1",
    description="受控业务工具真实执行次数",
)
# 人工接管计数用于观察自动化失败或安全降级比例。
human_handoff_counter = meter.create_counter(
    "serviceops.agent.handoffs",
    unit="1",
    description="Agent 转人工次数",
)
# 审批决定与终态计数用于监控批准、拒绝和失败分布。
approval_execution_counter = meter.create_counter(
    "serviceops.approval.executions",
    unit="1",
    description="人工审批恢复执行次数",
)
# Outbox 投递只按有限结果计数，帮助观察积压补偿、失败和死信趋势。
outbox_dispatch_counter = meter.create_counter(
    "serviceops.outbox.dispatches",
    unit="1",
    description="事务 Outbox 投递尝试结果",
)
# 后端工位排队超时只按固定原因计数，不把来源 IP、路径或用户放进 Metrics。
capacity_rejection_counter = meter.create_counter(
    "serviceops.http.capacity.rejections",
    unit="1",
    description="Agent 实例因并发容量耗尽而拒绝的业务请求数",
)
# 多轮影子观察只按有限意图、终态和解析原因聚合。
conversation_shadow_counter = meter.create_counter(
    "serviceops.conversation.shadow.observations",
    unit="1",
    description="多轮会话低敏影子观察数",
)
# 模型故障、证据拒答和上下文歧义使用有限signal标签。
conversation_shadow_signal_counter = meter.create_counter(
    "serviceops.conversation.shadow.signals",
    unit="1",
    description="多轮影子评测代理信号数",
)
# 安全红线使用固定违规码，不携带请求、会话或业务对象标识。
conversation_shadow_safety_counter = meter.create_counter(
    "serviceops.conversation.shadow.safety_violations",
    unit="1",
    description="多轮影子评测安全不变量违规数",
)

# 有限工具白名单也是 Metrics 属性白名单，未知值统一归一化避免基数爆炸。
SAFE_TOOL_NAMES = frozenset({"get_order_status", "create_return_request"})
# 意图白名单与领域枚举保持一致，但在可观测层不反向依赖分类实现。
SAFE_INTENTS = frozenset(
    {"faq", "order_status", "return_request", "human_handoff", "unknown"}
)
# 业务终态白名单用于有限指标标签。
SAFE_OUTCOMES = frozenset(
    {
        "completed",
        "approval_required",
        "human_handoff",
        "rejected",
        "failed",
        "clarification",
        "declined",
        "unknown",
    }
)
SAFE_SHADOW_RESOLUTION_REASONS = frozenset(
    {
        "explicit_reference",
        "verified_order_reference",
        "ambiguous_order_reference",
        "independent_question",
    }
)
SAFE_SHADOW_SIGNALS = frozenset(
    {
        "model_failure",
        "evidence_abstention",
        "ambiguous_context",
        "human_handoff",
        "safety_violation",
    }
)
SAFE_SHADOW_SAFETY_CODES = frozenset(
    {
        "ungrounded_faq_auto_answer",
        "approval_pending_contains_write_result",
        "model_failure_without_handoff",
        "active_order_missing_from_recent_orders",
        "cross_topic_active_order_retained",
    }
)

# 初始化锁和运行时缓存防止重复设置全局 Provider 产生 SDK 警告或重复导出。
_configuration_lock = Lock()
_telemetry_runtime: "TelemetryRuntime | None" = None


@dataclass(frozen=True)
class TelemetryRuntime:
    """进程级 Trace/Metrics Provider 以及生效导出器名称。"""

    # tracer_provider 管理采样、SpanProcessor 与 flush。
    tracer_provider: TracerProvider
    # meter_provider 管理指标聚合、周期 Reader 与 flush。
    meter_provider: MeterProvider
    # exporter 便于 readiness/文档确认当前 none/console/otlp_http 模式。
    exporter: str

    def force_flush(self, timeout_millis: int = 5_000) -> bool:
        """在进程退出或本地示例结束前尝试导出已缓冲的 Trace 和 Metrics。"""

        # Trace 与 Metrics 都必须成功才返回 True。
        trace_flushed = self.tracer_provider.force_flush(timeout_millis)
        # Metrics Provider 使用同样的毫秒超时。
        metrics_flushed = self.meter_provider.force_flush(timeout_millis)
        # bool(...) 消除第三方 SDK 返回类型对调用方的影响。
        return bool(trace_flushed and metrics_flushed)


def _sanitize_log_text(value: object, *, max_length: int = 1_000) -> str:
    """把日志值限制为单行短文本，避免 CRLF 注入与无边界输出。"""

    # str 只应用于调用方已经选择的安全字段，不用于 Token/SecretStr。
    text_value = str(value)
    # split/join 将换行、制表符和连续空白归一化。
    normalized_value = " ".join(text_value.split())
    # 截断防止异常长标识占满日志管道。
    return normalized_value[:max_length]


def current_trace_id() -> str | None:
    """返回当前有效 Trace ID 的 32 位十六进制字符串。"""

    # 当前 Span 可能是未记录的 NoOpSpan。
    span_context = trace.get_current_span().get_span_context()
    # 无有效上下文时返回 None，调用方不应生成伪 Trace ID。
    if not span_context.is_valid:
        return None
    # OpenTelemetry Trace ID 固定 128 bit，用前导零补足 32 位。
    return f"{span_context.trace_id:032x}"


def _current_span_id() -> str | None:
    """返回当前有效 Span ID 的 16 位十六进制字符串。"""

    # 与 current_trace_id 使用同一当前上下文。
    span_context = trace.get_current_span().get_span_context()
    # 无效上下文不能输出全零占位符混淆检索。
    if not span_context.is_valid:
        return None
    # Span ID 固定 64 bit。
    return f"{span_context.span_id:016x}"


class TraceJsonFormatter(logging.Formatter):
    """输出带 trace_id/span_id 的单行 JSON 日志。"""

    # 只有这些由项目代码显式提供的 extra 字段允许进入结构化日志。
    SAFE_EXTRA_FIELDS = (
        "request_id",
        "thread_id",
        "operation",
        "event_type",
        "outcome",
        "failure_code",
    )

    def format(self, record: logging.LogRecord) -> str:
        """把标准 LogRecord 转为最小化 JSON，不序列化任意 __dict__。"""

        # 基础字段保持稳定，便于 Loki/Elasticsearch/Cloud Logging 建索引。
        payload: dict[str, object] = {
            # UTC ISO 8601 时间避免多时区歧义。
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            # level 使用标准大写名称。
            "level": record.levelname,
            # logger_name 用于区分 API、图节点和仓库日志。
            "logger": record.name,
            # getMessage 完成参数插值后再做单行化和长度限制。
            "message": _sanitize_log_text(record.getMessage()),
        }
        # 当前请求存在有效 OTel 上下文时自动关联 Trace。
        trace_id = current_trace_id()
        span_id = _current_span_id()
        if trace_id is not None:
            payload["trace_id"] = trace_id
        if span_id is not None:
            payload["span_id"] = span_id
        # 只读取固定白名单 extra，拒绝把 LogRecord 任意字段整体序列化。
        for field_name in self.SAFE_EXTRA_FIELDS:
            field_value = getattr(record, field_name, None)
            if field_value is not None:
                payload[field_name] = _sanitize_log_text(field_value, max_length=200)
        # 异常只保留类型，不输出可能包含用户原文、服务商响应或秘密的异常消息/堆栈。
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        # ensure_ascii=False 让 PyCharm 正常显示中文；每条日志保持单行 JSON。
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _configure_json_logging(settings: Settings) -> None:
    """只配置 serviceops_agent 命名空间，不覆盖 Uvicorn 自己的访问日志策略。"""

    # 获取项目顶层 Logger，子模块会自然继承该 Handler。
    project_logger = logging.getLogger("serviceops_agent")
    # 使用 Settings 中的有限日志级别。
    project_logger.setLevel(settings.log_level)
    # 防止同一日志继续传播到 Root Logger 造成重复输出和不同格式副本。
    project_logger.propagate = False
    # 重复初始化时先移除本项目之前创建的 Handler。
    project_logger.handlers.clear()
    # StreamHandler 写入标准输出，容器运行时可以统一采集。
    handler = logging.StreamHandler(sys.stdout)
    # Handler 级别与项目 Logger 一致。
    handler.setLevel(settings.log_level)
    # 使用只输出安全白名单字段的 JSON Formatter。
    handler.setFormatter(TraceJsonFormatter())
    # 安装唯一 Handler。
    project_logger.addHandler(handler)


def _build_exporters(settings: Settings) -> tuple[SpanExporter | None, MetricExporter | None]:
    """根据有限配置创建成对 Trace/Metrics Exporter。"""

    # none 模式仍创建 Provider/API，但不启动后台导出线程。
    if settings.telemetry_exporter == "none":
        return None, None
    # console 用于本地学习，不需要 Collector。
    if settings.telemetry_exporter == "console":
        return ConsoleSpanExporter(), ConsoleMetricExporter()
    # Settings 已把剩余值限制为 otlp_http；统一去掉末尾斜杠避免双斜杠。
    endpoint_root = settings.otel_otlp_endpoint.rstrip("/")
    # OTLP/HTTP 的 Trace 与 Metrics 使用不同标准路径。
    return (
        OTLPSpanExporter(endpoint=f"{endpoint_root}/v1/traces"),
        OTLPMetricExporter(endpoint=f"{endpoint_root}/v1/metrics"),
    )


def _build_telemetry_resource(settings: Settings) -> Resource:
    """构造稳定、低基数的服务Resource，覆盖SDK随机实例标识。"""

    return Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
            "service.instance.id": settings.instance_id,
            "deployment.environment.name": settings.environment,
        }
    )


def configure_telemetry(settings: Settings) -> TelemetryRuntime | None:
    """幂等配置进程级 Provider、Exporter 与关联 JSON 日志。"""

    # 自动测试和明确关闭场景保留 NoOp API，不创建线程或修改日志 Handler。
    if not settings.telemetry_enabled:
        return None
    # 全局 Provider 只能可靠设置一次，因此进入互斥区。
    with _configuration_lock:
        # 声明将要写入模块缓存。
        global _telemetry_runtime
        # 已配置时直接复用，避免重复 Processor 和双倍指标。
        if _telemetry_runtime is not None:
            return _telemetry_runtime

        # Resource 只包含服务级低基数字段。
        resource = _build_telemetry_resource(settings)
        # 上游采样决定优先；没有父 Span 时使用本服务比例。
        sampler = ParentBased(TraceIdRatioBased(settings.otel_trace_sample_ratio))
        # 创建 Trace SDK Provider。
        tracer_provider = TracerProvider(resource=resource, sampler=sampler)
        # 根据配置构建导出器。
        span_exporter, metric_exporter = _build_exporters(settings)
        # 存在导出器时用 BatchProcessor 异步批量发送，避免请求线程同步网络 I/O。
        if span_exporter is not None:
            tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

        # none 模式没有 Reader；其他模式周期性导出聚合指标。
        metric_readers = []
        if metric_exporter is not None:
            metric_readers.append(
                PeriodicExportingMetricReader(
                    metric_exporter,
                    export_interval_millis=settings.otel_metric_export_interval_ms,
                )
            )
        # 创建 Metrics SDK Provider。
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=metric_readers,
        )
        # 设置全局 Provider 后，模块级 ProxyTracer/ProxyMeter 会自动委托到真实 SDK。
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        # 项目日志采用标准库 JSON Formatter 关联当前 Trace。
        _configure_json_logging(settings)
        # 缓存运行时供 FastAPI instrumentation 和退出 flush 使用。
        _telemetry_runtime = TelemetryRuntime(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            exporter=settings.telemetry_exporter,
        )
        return _telemetry_runtime


def force_flush_telemetry(timeout_millis: int = 5_000) -> bool:
    """刷新当前运行时；关闭遥测时直接返回 True。"""

    # 未配置表示没有任何后台缓冲需要导出。
    if _telemetry_runtime is None:
        return True
    # 委托不可变运行时对象。
    return _telemetry_runtime.force_flush(timeout_millis)


def record_capacity_rejection() -> None:
    """记录一次后端业务工位排队超时，不附加任何请求级或身份字段。"""

    # reason 是代码内固定低基数值，可用于告警而不会随用户或请求无限增长。
    capacity_rejection_counter.add(1, {"reason": "queue_timeout"})


def _safe_span_attributes(attributes: dict[str, object] | None) -> dict[str, Any]:
    """只保留 OTel 支持的基础类型，并限制自由字符串长度。"""

    # 无属性时避免创建多余字典分支。
    if attributes is None:
        return {}
    # OTel Span attribute 支持 bool/int/float/str；本项目不通过此入口传复杂对象。
    sanitized: dict[str, Any] = {}
    for key, value in attributes.items():
        # OTel 接受布尔和数值基础类型；复杂对象一律不进入 Span。
        if isinstance(value, (bool, int, float)):
            sanitized[key] = value
        elif isinstance(value, str):
            sanitized[key] = _sanitize_log_text(value, max_length=200)
    # 返回 SDK 可接受的安全字典。
    return sanitized


@contextmanager
def start_safe_span(
    name: str,
    *,
    attributes: dict[str, object] | None = None,
) -> Iterator[Span]:
    """创建不自动记录异常正文的当前 Span。"""

    # record_exception=False 防止第三方异常 message/stacktrace 自动进入遥测后端。
    with tracer.start_as_current_span(
        name,
        attributes=_safe_span_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            # 调用方在 with 内完成业务操作。
            yield span
        except GraphInterrupt:
            # interrupt 是可恢复工作流的预期暂停，而不是 ERROR。
            span.set_attribute("serviceops.graph.interrupted", True)
            # 必须继续抛给 LangGraph，让框架保存 Checkpoint 和中断负载。
            raise
        except Exception as error:
            # 只记录异常类型，不记录 str(error)。
            span.set_attribute("error.type", type(error).__name__)
            # 标记失败便于 Trace 后端筛选。
            span.set_status(Status(StatusCode.ERROR))
            # 原异常必须继续传播，遥测不能改变业务错误语义。
            raise


def add_current_span_attributes(attributes: dict[str, object]) -> None:
    """向当前有效且正在记录的 Span 添加经过限制的属性。"""

    # NoOpSpan 或未采样 Span 不做额外工作。
    span = trace.get_current_span()
    if not span.is_recording():
        return
    # 逐个写入可接受属性。
    for key, value in _safe_span_attributes(attributes).items():
        span.set_attribute(key, value)


def _normalize_intent(value: object) -> str:
    """把任意状态值归一化为有限指标意图。"""

    # StrEnum 的 str 表现和普通字符串都在这里统一处理。
    candidate = str(value)
    # 只保留固定白名单。
    return candidate if candidate in SAFE_INTENTS else "unknown"


def _normalize_outcome(result: dict[str, Any]) -> str:
    """从最终图状态推导有限业务结果，供 Trace 和 Metrics 共用。"""

    # 框架 interrupt 表示等待人工批准，并非错误。
    if result.get("__interrupt__"):
        return "approval_required"
    # 有限退货状态优先表达 rejected/failed/clarification/declined/completed。
    workflow_status = str(result.get("return_workflow_status", ""))
    if workflow_status in SAFE_OUTCOMES:
        return workflow_status
    # 普通人工接管路径使用布尔标记。
    if result.get("requires_human") is True:
        return "human_handoff"
    # 普通 FAQ/订单成功终态。
    if result.get("answer"):
        return "completed"
    # 未知结构保持有限 fallback，不把任意值变成指标标签。
    return "unknown"


def instrument_graph_node(
    node_name: str,
    node: Callable[[ServiceState], object],
) -> RunnableLambda[ServiceState, dict[str, object]]:
    """把同步或异步 LangGraph 节点包装为带耗时 Span 的异步节点。"""

    async def traced_node(state: ServiceState) -> dict[str, object]:
        """执行原节点，同时记录有限属性和低基数耗时指标。"""

        # perf_counter 只计算时长，不参与业务时间戳。
        started_at = perf_counter()
        # 节点 Span 继承当前 HTTP/Agent Span 上下文。
        with start_safe_span(
            f"serviceops.graph.node.{node_name}",
            attributes={
                # node.name 来自代码常量，不受用户输入影响。
                "serviceops.graph.node.name": node_name,
                # request_id 允许出现在 Trace 中用于单请求关联，但不会进入 Metrics。
                "serviceops.request.id": state.get("request_id", "unknown"),
            },
        ) as span:
            try:
                # 节点可能直接返回 dict，也可能返回 Awaitable。
                raw_result = node(state)
                # inspect.isawaitable 在调用处已经常用；这里直接使用 hasattr 协议会损失类型安全。
                if isinstance(raw_result, Awaitable):
                    raw_result = await raw_result
                # LangGraph 节点契约要求返回状态更新字典。
                if not isinstance(raw_result, dict):
                    raise TypeError("LangGraph 节点必须返回 dict 状态增量")
                # cast 恢复稳定返回类型；具体 TypedDict 会被 LangGraph 合并。
                result = cast(dict[str, object], raw_result)
                # Span 可携带有限结果属性，避免记录整份 State。
                span.set_attribute(
                    "serviceops.agent.intent",
                    _normalize_intent(result.get("intent", state.get("intent", "unknown"))),
                )
                if isinstance(result.get("requires_human"), bool):
                    span.set_attribute(
                        "serviceops.agent.requires_human",
                        cast(bool, result["requires_human"]),
                    )
                # 成功节点耗时只使用 node/outcome 两个有限标签。
                graph_node_duration.record(
                    (perf_counter() - started_at) * 1_000,
                    {"node.name": node_name, "outcome": "success"},
                )
                return result
            except GraphInterrupt:
                # 人工审批暂停使用独立有限结果，不能增加 error 指标。
                graph_node_duration.record(
                    (perf_counter() - started_at) * 1_000,
                    {"node.name": node_name, "outcome": "interrupted"},
                )
                raise
            except Exception:
                # start_safe_span 已负责 Span error.type/status；这里补充失败耗时指标。
                graph_node_duration.record(
                    (perf_counter() - started_at) * 1_000,
                    {"node.name": node_name, "outcome": "error"},
                )
                raise

    # RunnableLambda 让 LangGraph 明确识别输入/输出泛型，同时保留异步调用。
    return RunnableLambda(traced_node)


def record_agent_execution(
    *,
    operation: str,
    result: dict[str, Any],
    duration_ms: float,
) -> None:
    """记录一次对话/图恢复的低基数业务指标并丰富当前 Span。"""

    # operation 由 API 常量传入，仅允许 chat/approval。
    safe_operation = operation if operation in {"chat", "approval"} else "unknown"
    # 意图和结果统一归一化。
    intent = _normalize_intent(result.get("intent", "unknown"))
    outcome = _normalize_outcome(result)
    # 三个属性均为有限集合，适合 Metrics 聚合。
    metric_attributes = {
        "operation": safe_operation,
        "intent": intent,
        "outcome": outcome,
    }
    # 请求计数加一。
    agent_execution_counter.add(1, metric_attributes)
    # 记录端到端图执行耗时。
    agent_execution_duration.record(duration_ms, metric_attributes)
    # 真正执行的工具次数可能为零；计数器只在正数时增加。
    raw_tool_count = result.get("tool_call_count", 0)
    tool_count = raw_tool_count if isinstance(raw_tool_count, int) else 0
    raw_tool_name = str(result.get("tool_name", "unknown"))
    tool_name = raw_tool_name if raw_tool_name in SAFE_TOOL_NAMES else "unknown"
    if tool_count > 0:
        tool_call_counter.add(
            tool_count,
            {"tool.name": tool_name, "outcome": outcome},
        )
    # requires_human 是稳定业务布尔值，人工路径单独计数。
    if result.get("requires_human") is True:
        human_handoff_counter.add(1, {"intent": intent, "outcome": outcome})
    # Trace 可以关联高基数请求 ID，但仍不记录 user_id 或原始消息。
    add_current_span_attributes(
        {
            "serviceops.request.id": str(result.get("request_id", "unknown")),
            "serviceops.agent.intent": intent,
            "serviceops.agent.outcome": outcome,
            "serviceops.agent.tool_call_count": tool_count,
        }
    )


def record_approval_execution(*, approved: bool, outcome: str) -> None:
    """记录人工审批决定与有限终态分布。"""

    # outcome 必须落在有限集合，未知值归一化。
    safe_outcome = outcome if outcome in SAFE_OUTCOMES else "unknown"
    # 布尔决定作为低基数属性不会造成指标爆炸。
    approval_execution_counter.add(
        1,
        {"decision": "approved" if approved else "rejected", "outcome": safe_outcome},
    )


def record_conversation_shadow_observation(
    *,
    candidate_id: str,
    intent: str,
    outcome: str,
    resolution_reason: str,
    model_failure: bool,
    evidence_abstention: bool,
    ambiguous_context: bool,
    human_handoff: bool,
    safety_violation_codes: list[str],
) -> None:
    """导出一条无高基数标签的多轮影子观察。"""

    safe_candidate_id = (
        candidate_id
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", candidate_id)
        else "unknown"
    )
    safe_intent = _normalize_intent(intent)
    safe_outcome = outcome if outcome in SAFE_OUTCOMES else "unknown"
    safe_resolution = (
        resolution_reason
        if resolution_reason in SAFE_SHADOW_RESOLUTION_REASONS
        else "unknown"
    )
    conversation_shadow_counter.add(
        1,
        {
            "candidate_id": safe_candidate_id,
            "intent": safe_intent,
            "outcome": safe_outcome,
            "resolution.reason": safe_resolution,
        },
    )
    signals = {
        "model_failure": model_failure,
        "evidence_abstention": evidence_abstention,
        "ambiguous_context": ambiguous_context,
        "human_handoff": human_handoff,
        # 一个观察不论命中几个违规码都只计一次，供窗口安全违规率作分子。
        "safety_violation": bool(safety_violation_codes),
    }
    for signal, active in signals.items():
        if active and signal in SAFE_SHADOW_SIGNALS:
            conversation_shadow_signal_counter.add(
                1,
                {"candidate_id": safe_candidate_id, "signal": signal},
            )
    for code in sorted(set(safety_violation_codes)):
        safe_code = code if code in SAFE_SHADOW_SAFETY_CODES else "unknown"
        conversation_shadow_safety_counter.add(
            1,
            {"candidate_id": safe_candidate_id, "violation": safe_code},
        )


def record_outbox_dispatch(*, outcome: str) -> None:
    """记录一次 Outbox 投递的有限结果，不附加事件或线程标识。"""

    # 白名单防止异常字符串进入 Metrics 后形成高基数时间序列。
    safe_outcome = (
        outcome
        if outcome in {"processed", "replayed", "failed", "dead_letter"}
        else "failed"
    )
    # Counter 只包含单个有限标签，可直接计算成功、重放、失败和死信数量。
    outbox_dispatch_counter.add(1, {"outcome": safe_outcome})
