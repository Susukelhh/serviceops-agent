"""人工接管响应节点。

FAQ 已迁移到独立的 ``faq.py``，在那里执行证据检索和带引用回答；订单路径位于
``order.py``。本文件集中处理所有安全退出原因对应的用户文案。
"""

# Intent 用于区分 FAQ 知识覆盖不足和普通未知意图。
from serviceops_agent.domain.enums import Intent

# ServiceState 提供模型故障、RAG 故障、证据门和路由意图字段。
from serviceops_agent.graph.state import ServiceState


def handoff_to_human(state: ServiceState) -> dict[str, object]:
    """为当前无法可靠处理的问题返回人工接管说明。

    当前根据模型故障码区分“系统暂不可用”和普通未知意图；后续会把原始问题和路由原因
    写入真实人工工单。
    """

    # 模型故障码只在自动分类服务失败时存在；普通未知意图不会包含该字段。
    llm_failure_code = state.get("llm_failure_code")
    # RAG 故障码只在 Embedding 或向量库不可用时存在。
    rag_failure_code = state.get("rag_failure_code")
    # 无证据标记表示基础设施正常，但知识库覆盖或相关度不足。
    rag_no_evidence = state.get("rag_no_evidence", False)
    # Agent 故障码覆盖规划、工具白名单、重复调用、步数和身份安全边界。
    agent_failure_code = state.get("agent_failure_code")

    # 不同失败原因使用不同确定性文案，避免把系统问题错误归因给用户。
    if agent_failure_code == "missing_identity":
        # user_id 只能来自可信系统上下文，不能提示用户在自然语言中伪造身份。
        answer = "当前无法验证用户身份，暂时不能查询订单，请联系人工客服。"
    elif agent_failure_code in {"tool_execution_error", "invalid_tool_result"}:
        # 工具或领域结果异常使用厂商无关文案，不暴露仓库与异常正文。
        answer = "订单查询工具暂时不可用，本次请求已建议转交人工客服。"
    elif agent_failure_code:
        # 规划失败、越权工具、重复调用和步数耗尽统一说明自动执行未安全完成。
        answer = "自动工具执行未能安全完成，本次请求已建议转交人工客服。"
    elif llm_failure_code:
        # 模型故障时不暴露密钥、服务商或内部异常详情。
        answer = "自动处理服务暂时不可用，本次请求已建议转交人工客服。"
    elif rag_failure_code:
        # 生成阶段故障与 Embedding/向量检索故障使用不同文案，便于用户理解发生阶段。
        if rag_failure_code.startswith("generation_"):
            # 不区分结构化解析、模型超时或非法引用，避免暴露内部安全校验细节。
            answer = "知识回答服务暂时不可用，本次请求已建议转交人工客服。"
        else:
            # 检索基础设施故障与无知识命中是不同运维问题。
            answer = "企业知识检索服务暂时不可用，本次请求已建议转交人工客服。"
    elif rag_no_evidence or state.get("intent") == Intent.FAQ:
        # FAQ 无充分证据时明确拒绝猜测，并提示人工接管。
        answer = "知识库中暂未找到足够依据，本次请求已建议转交人工客服。"
    else:
        # 普通未知意图继续使用原有安全退出说明。
        answer = "当前信息不足以安全自动处理，系统已建议转交人工客服。"

    # 返回人工接管路径负责产生的状态增量。
    return {
        # 返回与实际转人工原因一致的确定性用户文案。
        "answer": answer,
        # 明确标记需要人工，API 和未来工单服务都会读取该值。
        "requires_human": True,
        # 追加人工转接事件，后续可统计自动化覆盖率与人工转接率。
        "events": ["graph:human_handoff_requested"],
    }
