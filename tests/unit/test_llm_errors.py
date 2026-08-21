"""模型 SDK 异常归一化的无网络单元测试。"""

# HTTPX Request/Response 用于构造 OpenAI SDK 异常要求的最小 HTTP 上下文。
from httpx import Request, Response

# 这些异常也是千问 OpenAI 兼容接口经过 SDK 转换后呈现给项目的类型。
from openai import APITimeoutError, AuthenticationError, BadRequestError, RateLimitError

# 被测函数负责隐藏原始服务商错误，并返回稳定的有限故障类别。
from serviceops_agent.llm.errors import LLMFailureKind, normalize_llm_exception


def _create_response(status_code: int) -> Response:
    """创建带请求信息的最小 HTTP 响应，满足 OpenAI SDK 异常构造要求。"""

    # Request 只包含固定测试 URL，不会真正发送任何网络流量。
    request = Request("POST", "https://model-provider.test/v1/chat/completions")
    # Response 保存状态码和关联请求，供 SDK 异常读取 request_id 等上下文。
    return Response(status_code=status_code, request=request)


def test_normalize_authentication_error_hides_provider_message() -> None:
    """认证错误应标记为不可重试，并且内部消息不得复制服务商原文。"""

    # Arrange：模拟服务商返回包含敏感占位文本的 401 响应。
    provider_error = AuthenticationError(
        "provider-secret-detail",
        # 401 响应帮助 SDK 确定异常类型，测试不访问外部网络。
        response=_create_response(401),
        # body 模拟服务商错误体；归一化结果不应保存它。
        body={"message": "provider-secret-detail"},
    )

    # Act：把具体 SDK 异常转换成项目内部错误。
    normalized = normalize_llm_exception(provider_error)

    # Assert：401 被准确识别为认证故障，而不是通用上游错误。
    assert normalized.kind == LLMFailureKind.AUTHENTICATION
    # Assert：修改密钥前立即重试不会恢复，因此标记为不可重试。
    assert normalized.retryable is False
    # Assert：稳定内部异常文本没有复制模拟的敏感服务商消息。
    assert "provider-secret-detail" not in str(normalized)


def test_normalize_rate_limit_error_is_retryable() -> None:
    """限流错误应保留适合稍后重试的策略信息。"""

    # Arrange：构造服务商返回的 429 限流异常。
    provider_error = RateLimitError(
        "too many requests",
        # 429 是标准限流状态码。
        response=_create_response(429),
        # 测试错误体不需要真实服务商字段。
        body=None,
    )

    # Act：执行统一异常映射。
    normalized = normalize_llm_exception(provider_error)

    # Assert：类别能够用于独立统计模型限流率。
    assert normalized.kind == LLMFailureKind.RATE_LIMIT
    # Assert：等待配额窗口恢复后可能成功，因此标记可稍后重试。
    assert normalized.retryable is True


def test_normalize_timeout_error_is_retryable() -> None:
    """客户端超时应区别于连接错误和服务商 HTTP 错误。"""

    # Arrange：使用固定请求对象构造不会进行网络调用的 SDK 超时异常。
    provider_error = APITimeoutError(
        Request("POST", "https://model-provider.test/v1/chat/completions")
    )

    # Act：把 SDK 超时转换为内部错误。
    normalized = normalize_llm_exception(provider_error)

    # Assert：独立 timeout 类别可用于后续 P95/P99 延迟告警。
    assert normalized.kind == LLMFailureKind.TIMEOUT
    # Assert：服务恢复或延迟下降后可能成功，因此允许稍后重试。
    assert normalized.retryable is True


def test_normalize_other_http_error_as_upstream_failure() -> None:
    """除认证和限流外的 HTTP 错误应归为通用上游故障。"""

    # Arrange：400 可代表模型名错误或不支持的结构化输出参数。
    provider_error = BadRequestError(
        "bad request",
        # 提供固定 400 响应，不发送网络请求。
        response=_create_response(400),
        # body 在归一化后不会进入 State 或用户响应。
        body={"message": "bad request"},
    )

    # Act：执行异常归一化。
    normalized = normalize_llm_exception(provider_error)

    # Assert：通用非成功 HTTP 状态映射为 upstream。
    assert normalized.kind == LLMFailureKind.UPSTREAM
    # Assert：400 通常需要修改配置或请求，当前保守标记为不可重试。
    assert normalized.retryable is False
