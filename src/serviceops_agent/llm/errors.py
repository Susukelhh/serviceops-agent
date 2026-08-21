"""把不同模型 SDK 的异常归一化为项目内部稳定错误。

LangGraph 节点不应该认识 OpenAI、千问或其他服务商的异常类，否则每增加一个模型供应商，
业务控制流都要跟着修改。本模块位于模型适配层：它读取具体 SDK 异常，然后只向上层暴露
有限的 ``LLMFailureKind`` 和 ``LLMServiceError``。
"""

# StrEnum 让错误类别既有枚举约束，又能稳定写入 State、日志和执行事件。
from enum import StrEnum

# LangChain 在结构化输出无法解析时会抛出 OutputParserException。
from langchain_core.exceptions import OutputParserException

# OpenAI 兼容客户端会把千问等服务商的 HTTP 错误转换成这些统一异常类型。
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)

# Pydantic 在模型输出不符合 IntentClassification Schema 时抛出 ValidationError。
from pydantic import ValidationError


class LLMFailureKind(StrEnum):
    """状态图允许识别和统计的有限模型故障类别。"""

    # AUTHENTICATION 表示密钥无效、密钥被撤销或密钥与服务地址不匹配。
    AUTHENTICATION = "authentication"
    # RATE_LIMIT 表示额度、并发或请求速率达到服务商限制。
    RATE_LIMIT = "rate_limit"
    # TIMEOUT 表示模型在应用配置的最长等待时间内没有完成响应。
    TIMEOUT = "timeout"
    # CONNECTION 表示 DNS、TLS、代理或网络连接层面的失败。
    CONNECTION = "connection"
    # INVALID_RESPONSE 表示模型响应无法通过结构化输出或 Pydantic 校验。
    INVALID_RESPONSE = "invalid_response"
    # UPSTREAM 表示服务商返回了其他非成功 HTTP 状态，例如 400、404 或 500。
    UPSTREAM = "upstream"
    # UNKNOWN 是适配器边界内未被 SDK 明确分类的保守兜底类别。
    UNKNOWN = "unknown"


class LLMServiceError(RuntimeError):
    """提供给 LangGraph 节点的脱敏模型服务异常。

    原始 SDK 异常通过 Python 的异常链 ``raise ... from ...`` 保留，只供服务端诊断；
    本异常自身不保存服务商响应正文，防止上游错误信息意外包含密钥或敏感请求数据。
    """

    def __init__(self, kind: LLMFailureKind, *, retryable: bool) -> None:
        """保存稳定错误类别和是否适合稍后重试的运维属性。"""

        # kind 用于选择降级事件、聚合监控指标和定位故障类型。
        self.kind = kind
        # retryable 只描述“稍后重试是否可能成功”，本阶段不会在图内盲目重复收费调用。
        self.retryable = retryable
        # RuntimeError 仅接收脱敏后的内部描述，不拼接原始服务商异常文本。
        super().__init__(f"LLM service failure: {kind.value}")


def normalize_llm_exception(error: Exception) -> LLMServiceError:
    """把模型适配器捕获的任意异常转换为稳定、脱敏的内部错误。

    判断顺序必须从具体子类到通用父类。例如 AuthenticationError 和 RateLimitError 都属于
    APIStatusError，如果先判断 APIStatusError，就会丢失更精确的认证与限流分类。
    """

    # 已经归一化的异常直接返回，避免重复包装时丢失原有类别。
    if isinstance(error, LLMServiceError):
        # 返回同一个实例，使上层仍能访问最初设置的 retryable 属性。
        return error
    # 认证错误通常需要人工修复密钥或 Base URL，原请求立即重试不会恢复。
    if isinstance(error, AuthenticationError):
        # 返回不可重试的认证类别，但不复制服务商响应正文。
        return LLMServiceError(LLMFailureKind.AUTHENTICATION, retryable=False)
    # 限流或余额阈值可能随时间、配额恢复，因此标记为适合稍后重试。
    if isinstance(error, RateLimitError):
        # 当前请求仍然安全转人工，不在节点内部自动产生额外模型费用。
        return LLMServiceError(LLMFailureKind.RATE_LIMIT, retryable=True)
    # 超时需要单独统计，以便后续根据 P95/P99 延迟调整超时和降级策略。
    if isinstance(error, APITimeoutError):
        # 网络恢复或服务负载下降后通常可以成功，因此允许稍后重试。
        return LLMServiceError(LLMFailureKind.TIMEOUT, retryable=True)
    # 连接错误没有收到有效 HTTP 响应，通常属于短暂基础设施故障。
    if isinstance(error, APIConnectionError):
        # 标记可重试只提供策略信息，本函数本身不会执行重试。
        return LLMServiceError(LLMFailureKind.CONNECTION, retryable=True)
    # 其他 APIStatusError 表示服务商明确返回非 2xx HTTP 状态。
    if isinstance(error, APIStatusError):
        # 是否可重试取决于具体状态，本阶段保守交由后续重试策略决定。
        return LLMServiceError(LLMFailureKind.UPSTREAM, retryable=False)
    # 解析失败和 Pydantic 校验失败都表示响应到达了，但不满足系统要求的 Schema。
    if isinstance(error, (OutputParserException, ValidationError)):
        # 同一次输入盲目重试可能继续返回相同坏格式，所以当前标记为不可重试。
        return LLMServiceError(LLMFailureKind.INVALID_RESPONSE, retryable=False)
    # 未知异常仍在模型适配器边界内安全归一化，避免第三方 SDK 细节击穿 HTTP 层。
    return LLMServiceError(LLMFailureKind.UNKNOWN, retryable=False)
