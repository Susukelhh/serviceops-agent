"""创建 LangGraph Checkpointer 使用的显式安全序列化器。

LangGraph 的 JsonPlusSerializer 可以恢复项目自定义 Pydantic 类型，但新版本要求应用明确
声明允许恢复哪些模块和类。显式白名单比允许任意模块更安全，也避免读取状态历史时出现
“未来版本将阻止该类型”的警告。
"""

# JsonPlusSerializer 是 LangGraph Checkpoint 官方提供的 JSON/MessagePack 序列化器。
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

# 这些类确实会以强类型对象进入 ServiceState。其余退货/审批字段在写入前已转换为 JSON 字典。
CHECKPOINT_ALLOWED_MSGPACK_TYPES = (
    # 订单规划与工具观察使用的有限领域类型。
    ("serviceops_agent.domain.agent", "AgentAction"),
    ("serviceops_agent.domain.agent", "ToolCallPlan"),
    ("serviceops_agent.domain.agent", "ToolExecutionRecord"),
    # FAQ 检索和引用使用的有限领域类型。
    ("serviceops_agent.domain.knowledge", "Citation"),
    ("serviceops_agent.domain.knowledge", "KnowledgeChunk"),
    ("serviceops_agent.domain.knowledge", "RetrievalHit"),
)


def create_checkpoint_serializer() -> JsonPlusSerializer:
    """为一个 Saver 创建只允许项目所需领域类型的独立序列化器。"""

    # 不开启 pickle_fallback；pickle 可执行任意对象构造，不适合持久化不可信数据库内容。
    return JsonPlusSerializer(
        pickle_fallback=False,
        # 精确到模块与类名；不能使用 True 放开所有可导入类型。
        allowed_msgpack_modules=CHECKPOINT_ALLOWED_MSGPACK_TYPES,
    )
