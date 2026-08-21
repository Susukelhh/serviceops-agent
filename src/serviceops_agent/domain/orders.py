"""订单查询工具使用的领域模型。

领域模型只描述订单数据和工具结果，不包含文件读取、LangChain Tool 或 LangGraph 节点逻辑，
因此未来把模拟仓库替换为真实订单服务时，这些稳定类型仍然可以复用。
"""

# datetime 用于表达订单状态最后更新时间，避免使用含义不明确的普通字符串。
from datetime import datetime

# StrEnum 让状态同时具备枚举约束和自然的 JSON 字符串序列化能力。
from enum import StrEnum

# BaseModel 提供运行时校验；Field 用正则和说明约束订单号等字段。
from pydantic import BaseModel, Field


class OrderStatus(StrEnum):
    """当前模拟订单系统支持的有限订单状态。"""

    # PAID 表示订单已支付，但仓库尚未完成发货。
    PAID = "paid"
    # SHIPPED 表示商品已交给承运商，可以返回物流单号。
    SHIPPED = "shipped"
    # DELIVERED 表示承运商已完成签收。
    DELIVERED = "delivered"
    # CANCELLED 表示订单已经取消，不应继续展示配送进度。
    CANCELLED = "cancelled"


class OrderRecord(BaseModel):
    """订单仓库内部保存的一条完整记录。"""

    # order_id 是对外订单号；本项目示例统一采用 `SO` 加六位数字的格式。
    order_id: str = Field(pattern=r"^SO\d{6}$", description="企业订单唯一编号")
    # user_id 表示订单归属；仓库查询时必须与当前登录用户匹配，防止越权读取。
    user_id: str = Field(min_length=1, max_length=64, description="订单所属用户标识")
    # status 是受枚举约束的当前订单状态，下游不会处理未知自由文本。
    status: OrderStatus
    # carrier 是承运商名称；未发货订单没有承运商，因此允许为 None。
    carrier: str | None = None
    # tracking_number 是物流单号；只有已发货等场景才可能存在。
    tracking_number: str | None = None
    # updated_at 记录状态最后更新时间，回答用户时可说明信息的新鲜程度。
    updated_at: datetime


class OrderLookupResult(BaseModel):
    """订单查询工具返回给图节点或模型的结构化结果。"""

    # found 表示是否找到了“属于当前用户”的订单，故意不区分不存在和无权限。
    found: bool
    # order_id 回显调用方查询的订单号，便于一次任务中关联多次工具结果。
    order_id: str
    # status 是查到订单后的有限状态；查询失败时为 None。
    status: OrderStatus | None = None
    # status_label 是面向中文用户的状态名称；查询失败时为 None。
    status_label: str | None = None
    # carrier 是允许向当前用户返回的承运商；查询失败或未发货时为 None。
    carrier: str | None = None
    # tracking_number 是允许向当前用户返回的物流号；查询失败或未发货时为 None。
    tracking_number: str | None = None
    # updated_at 是可序列化的 ISO 时间文本；查询失败时不提供。
    updated_at: str | None = None
    # message 是可直接展示的安全说明，不会泄露其他用户的订单是否真实存在。
    message: str
