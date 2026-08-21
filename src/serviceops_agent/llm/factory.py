"""根据配置选择关键词基线或真实 LLM 分类节点。"""

# Awaitable/Callable 用于表达 StateGraph 同时接受同步基线节点和异步模型节点。
from collections.abc import Awaitable, Callable

# Settings/get_settings 提供当前后端开关和置信度阈值。
from serviceops_agent.config.settings import Settings, get_settings

# 关键词分类节点保证默认环境、离线开发和全部测试无需模型密钥即可运行。
from serviceops_agent.graph.nodes.classifier import classify_intent

# ServiceState 是两种分类节点共同使用的输入状态类型。
from serviceops_agent.graph.state import ServiceState

# 真实客户端负责把 BaseChatModel 绑定为 Pydantic 结构化输出。
from serviceops_agent.llm.intent_classifier import (
    LangChainIntentClassificationClient,
    create_llm_intent_classifier_node,
)

# 模型工厂只在 openai_compatible 模式下创建实际网络客户端。
from serviceops_agent.llm.provider import create_chat_model

# StateUpdate 是节点返回并由 LangGraph 合并的部分状态字典。
type StateUpdate = dict[str, object]
# IntentClassifierNode 同时允许同步关键词函数和异步模型闭包。
type IntentClassifierNode = (
    Callable[[ServiceState], StateUpdate] | Callable[[ServiceState], Awaitable[StateUpdate]]
)


def build_intent_classifier_node(settings: Settings | None = None) -> IntentClassifierNode:
    """返回当前配置对应的 LangGraph 意图分类节点。"""

    # 显式传入 Settings 便于单元测试；生产代码未传入时读取缓存的全局配置。
    current_settings = settings or get_settings()
    # mock 模式直接返回确定性关键词节点，不初始化模型，也不会产生任何网络费用。
    if current_settings.llm_backend == "mock":
        # 保留基线有助于评测真实 LLM 是否确实提升了分类效果。
        return classify_intent

    # openai_compatible 模式先创建统一聊天模型。
    model = create_chat_model(current_settings)
    # 再把聊天模型适配为只做 IntentClassification 的结构化客户端。
    client = LangChainIntentClassificationClient(model)
    # 最后创建带置信度安全门的异步 LangGraph 节点。
    return create_llm_intent_classifier_node(
        # client 封装真实模型调用和 Pydantic 输出校验。
        client=client,
        # 阈值来自配置，后续可通过评测集而不是拍脑袋调整。
        confidence_threshold=current_settings.intent_confidence_threshold,
    )
