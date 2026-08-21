"""第一版意图分类节点。"""

# Intent 是节点允许输出的有限意图集合，避免产生下游无法识别的字符串。
from serviceops_agent.domain.enums import Intent

# ServiceState 为节点输入提供字段类型提示和 PyCharm 自动补全。
from serviceops_agent.graph.state import ServiceState

# 关键词分类只是一个可重复、无密钥的基线。第二步会用模型结构化输出替换它，
# 但仍保留这份基线用于比较模型方案是否真的提升了效果。
# FAQ_KEYWORDS 收集知识型问题的最小触发词，用于建立可重复执行的分类基线。
FAQ_KEYWORDS = ("保修", "发票", "退换货政策", "售后政策", "营业时间")
# ORDER_KEYWORDS 收集订单和物流问题触发词，命中后进入订单查询路径。
ORDER_KEYWORDS = ("订单", "物流", "快递", "发货", "到哪里", "到哪了")
# RETURN_REQUEST_KEYWORDS 只匹配明确创建动作，普通“退货政策”仍进入只读 FAQ。
RETURN_REQUEST_KEYWORDS = ("申请退货", "发起退货", "我要退货", "退货申请")


def classify_intent(state: ServiceState) -> dict[str, object]:
    """将规范化后的文本映射为有限的业务意图。

    真正接入大模型后也不会让模型自由输出任意字符串，而会约束为同一个 `Intent` 枚举，
    以减少下游路由出现未知分支的概率。
    """

    # 读取预处理节点产生的文本；若字段意外缺失，空字符串会自然落入人工路径。
    message = state.get("normalized_message", "")

    # 写操作必须优先于 FAQ/订单查询判断，避免“订单 + 申请退货”被降级为只读路径。
    if any(keyword in message for keyword in RETURN_REQUEST_KEYWORDS):
        # 返回退货审批路径需要的分类状态。
        return {
            # RETURN_REQUEST 会被路由到草案准备和 interrupt 审批子图。
            "intent": Intent.RETURN_REQUEST,
            # 明确动作关键词命中时基线置信度约定为 1。
            "intent_confidence": 1.0,
            # 路由原因说明这是写操作，而不是普通政策咨询。
            "route_reason": "命中明确退货申请写操作关键词",
            # 真正的人为审批发生在后续 interrupt；分类阶段先允许进入草案节点。
            "requires_human": False,
            # 参数完整性由草案节点检查。
            "needs_clarification": False,
            # 事件明确记录高风险业务意图。
            "events": ["graph:intent_classified_as_return_request"],
        }

    # any 会逐个检查 FAQ 关键词，只要有一个关键词出现在文本中就视为 FAQ 候选。
    if any(keyword in message for keyword in FAQ_KEYWORDS):
        # 返回 FAQ 路径需要的全部状态增量，但不会重新返回无关的原始状态字段。
        return {
            # 写入有限枚举，供条件路由函数选择下一跳。
            "intent": Intent.FAQ,
            # 关键词明确命中时把基线置信度约定为 1，便于与模型结果使用同一字段比较。
            "intent_confidence": 1.0,
            # 记录为什么选择此路径，方便面试演示、错误分析和后续审计。
            "route_reason": "命中售后政策类关键词",
            # FAQ 是只读问答，本阶段无需人工介入。
            "requires_human": False,
            # 事件列表会通过 State 中的 add Reducer 追加，而不是覆盖旧事件。
            "events": ["graph:intent_classified_as_faq"],
        }

    # 只有未命中 FAQ 时才检查订单关键词，避免一个请求在本节点产生两个意图。
    if any(keyword in message for keyword in ORDER_KEYWORDS):
        # 返回订单路径需要的状态增量。
        return {
            # ORDER_STATUS 会被路由函数转换为 `order` 路由键。
            "intent": Intent.ORDER_STATUS,
            # 关键词明确命中时把基线置信度约定为 1。
            "intent_confidence": 1.0,
            # 保存当前基线分类器采用的可解释判断依据。
            "route_reason": "命中订单或物流类关键词",
            # 当前订单路径只做查询，不需要人工审批；写操作阶段会重新评估风险。
            "requires_human": False,
            # 记录分类结果，供集成测试验证完整执行路径。
            "events": ["graph:intent_classified_as_order_status"],
        }

    # 不确定请求进入人工路径，是当前阶段最简单的风险控制策略。
    # 这里是所有未命中已知关键词请求的安全兜底状态增量。
    return {
        # 显式写入人工意图，而不是让 intent 保持缺失，方便后续统计人工转接率。
        "intent": Intent.HUMAN_HANDOFF,
        # 未命中任何规则时基线没有分类把握，因此置信度记为 0。
        "intent_confidence": 0.0,
        # 说明转人工是因为证据不足，而不是系统发生异常。
        "route_reason": "当前分类器没有足够证据自动处理",
        # API 会把该标记返回给调用方，未来工单系统也会据此创建人工任务。
        "requires_human": True,
        # 记录安全兜底事件，便于评测未知问题是否被正确拦截。
        "events": ["graph:intent_classified_as_human_handoff"],
    }
