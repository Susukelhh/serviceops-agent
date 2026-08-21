"""可观测性 JSON 日志关联、敏感字段忽略和生产配置安全门测试。"""

# json 解析 Formatter 输出；logging 构造标准 LogRecord。
import json
import logging

# pytest 提供异常断言。
import pytest

# NonRecordingSpan/use_span 在不设置全局 SDK Provider 的情况下模拟当前 Trace。
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
    use_span,
)

# ValidationError 验证生产环境组合配置在启动期失败。
from pydantic import ValidationError

# Settings 是生产遥测导出器安全门的测试目标。
from serviceops_agent.config.settings import Settings

# Formatter/current_trace_id 是不需要后台线程的纯可观测边界。
from serviceops_agent.observability.telemetry import (
    TraceJsonFormatter,
    current_trace_id,
)


def _log_record(message: str) -> logging.LogRecord:
    """创建带可控消息的最小标准 LogRecord。"""

    # LogRecord 的 pathname/lineno 仅满足标准库构造，不会被安全 Formatter 输出。
    return logging.LogRecord(
        name="serviceops_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_json_formatter_correlates_trace_and_ignores_arbitrary_secret_fields() -> None:
    """日志应关联 Trace、清理换行，但不能序列化任意 LogRecord extra。"""

    # Arrange：构造固定有效 Trace/Span ID，不配置真实 Exporter。
    span_context = SpanContext(
        trace_id=int("1" * 32, 16),
        span_id=int("2" * 16, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    span = NonRecordingSpan(span_context)
    record = _log_record("第一行\r\n第二行")
    # 白名单字段允许进入输出，但同样需要清理换行。
    record.request_id = "request-001\nforged-line"
    # 任意 api_key 即使被错误附加到 LogRecord，也不在 Formatter 白名单中。
    record.api_key = "should-never-be-serialized"

    # Act：把模拟 Span 设为当前上下文并格式化日志。
    with use_span(span, end_on_exit=False):
        assert current_trace_id() == "1" * 32
        serialized_log = TraceJsonFormatter().format(record)

    # 解析单行 JSON 后验证稳定字段。
    payload = json.loads(serialized_log)
    assert payload["trace_id"] == "1" * 32
    assert payload["span_id"] == "2" * 16
    # CRLF 被归一为空格，不能伪造第二条日志。
    assert payload["message"] == "第一行 第二行"
    assert payload["request_id"] == "request-001 forged-line"
    # 未授权 extra 永远不会被整体序列化。
    assert "api_key" not in payload
    assert "should-never-be-serialized" not in serialized_log


def test_json_formatter_records_only_exception_type_not_sensitive_message() -> None:
    """异常日志只能出现异常类型，不应输出可能敏感的异常 message/stacktrace。"""

    # Arrange：捕获一条故意包含敏感文本的异常三元组。
    try:
        raise ValueError("secret-model-response-body")
    except ValueError as error:
        exception_info = (type(error), error, error.__traceback__)
    record = _log_record("模型调用失败")
    record.exc_info = exception_info

    # Act：执行安全格式化。
    serialized_log = TraceJsonFormatter().format(record)
    payload = json.loads(serialized_log)

    # Assert：保留可聚合类型，但删除异常消息和堆栈正文。
    assert payload["exception_type"] == "ValueError"
    assert "secret-model-response-body" not in serialized_log


def test_production_rejects_console_telemetry_exporter() -> None:
    """生产环境不能把高流量 Trace/Metrics 直接打印到进程控制台。"""

    # Act/Assert：使用非默认安全密钥，让失败原因精准落在 telemetry_exporter。
    with pytest.raises(ValidationError, match="生产环境遥测导出器不能使用 console"):
        Settings(
            environment="production",
            jwt_secret_key="production-test-jwt-secret-that-is-long-enough-2026",
            telemetry_enabled=True,
            telemetry_exporter="console",
        )
