"""售后工单领域中的稳定枚举。"""

# StrEnum 让枚举成员既具有枚举约束，又能像字符串一样被 Pydantic 序列化为 JSON。
from enum import StrEnum


class Intent(StrEnum):
    """第一阶段支持的最小意图集合。

    后续不会为了展示能力无限增加意图，而是根据评测集中的真实问题逐步扩展。
    """

    # FAQ 表示政策、保修、发票等知识型问题，后续将进入 RAG 检索路径。
    FAQ = "faq"
    # ORDER_STATUS 表示订单或物流查询，后续将进入受控的业务工具调用路径。
    ORDER_STATUS = "order_status"
    # RETURN_REQUEST 表示明确要求创建退货申请的写操作，必须经过可恢复人工审批。
    RETURN_REQUEST = "return_request"
    # HUMAN_HANDOFF 表示证据不足，系统应停止自动处理并请求人工客服接管。
    HUMAN_HANDOFF = "human_handoff"
