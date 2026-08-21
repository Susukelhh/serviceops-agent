"""根据项目配置创建可替换的聊天模型。

当前只实现 OpenAI Chat Completions 兼容接口，因为许多国内外服务商都提供这一协议。
模型对象只在应用启动时创建，不会在每个请求中重复初始化客户端。
"""

# init_chat_model 是 LangChain 1.x 推荐的统一模型初始化入口。
from langchain.chat_models import init_chat_model

# BaseChatModel 是所有聊天模型共同基类，让上层不依赖具体 ChatOpenAI 实现。
from langchain_core.language_models.chat_models import BaseChatModel

# Settings 集中提供模型名、密钥、地址、超时和重试配置。
from serviceops_agent.config.settings import Settings


def create_chat_model(settings: Settings) -> BaseChatModel:
    """为 `openai_compatible` 后端创建一个 LangChain 聊天模型。

    Args:
        settings: 已通过 Pydantic 校验的应用配置。

    Returns:
        支持异步调用和结构化输出的 LangChain BaseChatModel。

    Raises:
        ValueError: 后端类型错误，或真实模型所需配置缺失。
    """

    # 该工厂不负责 mock 模式，误调用时立即报出清晰错误，避免静默使用错误模型。
    if settings.llm_backend != "openai_compatible":
        # 错误信息包含当前配置值，帮助在 PyCharm 控制台中快速定位环境变量问题。
        raise ValueError(f"当前后端 {settings.llm_backend!r} 不需要创建真实聊天模型")

    # SecretStr 只有显式调用 get_secret_value 才能取得明文，普通日志不会泄漏密钥。
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    # 空密钥不能发起真实请求，因此在创建客户端前进行失败快速检查。
    if not api_key:
        # 提示用户在本地 `.env` 配置，而不是把密钥硬编码进源码。
        raise ValueError("使用 openai_compatible 时必须配置 SERVICEOPS_LLM_API_KEY")
    # 占位模型名说明用户尚未完成配置，继续启动只会产生难理解的服务端错误。
    if settings.llm_model == "replace-with-model-name":
        # 明确指出缺失的环境变量，降低首次接入真实模型的排错成本。
        raise ValueError("请通过 SERVICEOPS_LLM_MODEL 配置真实模型名称")
    # OpenAI 兼容后端必须指定服务地址，避免请求意外发往错误服务商。
    if not settings.llm_base_url:
        # 不在仓库中写死厂商 URL，用户应从所选服务商官方控制台复制地址。
        raise ValueError("使用 openai_compatible 时必须配置 SERVICEOPS_LLM_BASE_URL")

    # 使用统一初始化入口创建模型；model_provider 固定为 openai 以采用兼容协议。
    return init_chat_model(
        # 具体模型标识由服务商决定，例如其控制台展示的聊天模型名称。
        model=settings.llm_model,
        # 指示 LangChain 加载 OpenAI 协议集成，底层依赖由 `langchain[openai]` 提供。
        model_provider="openai",
        # 密钥只传给模型客户端，不写入 State、响应或日志。
        api_key=api_key,
        # 自定义基础地址使同一套代码可以连接不同的 OpenAI 兼容服务。
        base_url=settings.llm_base_url,
        # 分类任务使用低温度，减少相同输入多次运行得到不同意图的概率。
        temperature=settings.llm_temperature,
        # 超时限制保证外部模型异常时请求能够结束并进入后续降级策略。
        timeout=settings.llm_timeout_seconds,
        # 重试只处理暂时性请求失败；业务校验失败不会依靠盲目重试解决。
        max_retries=settings.llm_max_retries,
    )
