"""只读订单状态查询工具。

模型可控制的参数只有 order_id；当前登录 user_id 由系统通过闭包注入，不进入 Tool Schema，
因此模型无法伪造其他用户身份来查询订单。
"""

# BaseTool 是工具对象的共同类型；tool 装饰器会根据类型和 docstring 生成工具 Schema。
from langchain.tools import BaseTool, tool

# BaseModel/Field 校验工具入参；field_validator 在正则校验前统一订单号大小写。
from pydantic import BaseModel, Field, field_validator

# OrderLookupResult 保证工具返回结构稳定；OrderStatus 用于中文状态标签映射。
from serviceops_agent.domain.orders import OrderLookupResult, OrderStatus

# OrderRepository 隐藏模拟 JSON 或未来真实数据库的具体实现。
from serviceops_agent.infrastructure.order_repository import OrderRepository

# 中文标签只负责展示，不改变底层有限 OrderStatus 枚举。
ORDER_STATUS_LABELS: dict[OrderStatus, str] = {
    # 已支付但未发货。
    OrderStatus.PAID: "已支付",
    # 已交给承运商配送。
    OrderStatus.SHIPPED: "已发货",
    # 已完成签收。
    OrderStatus.DELIVERED: "已签收",
    # 已取消，不再配送。
    OrderStatus.CANCELLED: "已取消",
}


class OrderLookupInput(BaseModel):
    """模型在调用订单工具时唯一可以填写的参数。"""

    # pattern 限制最终格式为 SO 加六位数字，阻止任意查询表达式进入仓库层。
    order_id: str = Field(pattern=r"^SO\d{6}$", description="要查询的订单号，例如 SO100001")

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> object:
        """在正则校验前去除订单号两侧空格并转为大写。"""

        # 只有字符串才执行规范化；其他类型交给 Pydantic 产生标准字段错误。
        if isinstance(value, str):
            # strip 处理复制时的多余空格，upper 允许用户输入小写 so 前缀。
            return value.strip().upper()
        # 原样返回非字符串，让 Pydantic 统一负责类型校验。
        return value


def create_order_status_tool(user_id: str, repository: OrderRepository) -> BaseTool:
    """为当前用户创建一个身份已绑定的只读订单工具。"""

    # 工具名称会进入模型看到的工具列表，使用稳定、明确的动词加业务对象命名。
    @tool("get_order_status", args_schema=OrderLookupInput)
    def get_order_status(order_id: str) -> dict[str, object]:
        """查询当前登录用户自己的订单状态和物流信息；不能查询其他用户订单。"""

        # user_id 来自外层系统闭包而非模型参数，从调用边界阻止模型伪造身份。
        order = repository.get_for_user(order_id=order_id, user_id=user_id)
        # 不存在或不属于当前用户都进入同一个分支，避免通过响应差异枚举他人订单。
        if order is None:
            # 构建受 Pydantic 校验的失败结果，字段结构与成功结果保持一致。
            result = OrderLookupResult(
                # found=False 是上层选择安全提示的唯一依据。
                found=False,
                # 回显已经过工具 Schema 校验的订单号。
                order_id=order_id,
                # 文案故意合并“不存在”和“无权限”两种情况，避免数据泄露。
                message="未找到该订单，或该订单不属于当前用户，请核对订单号。",
            )
            # mode=json 把枚举和时间等值转换为可传给模型或 JSON 响应的基础类型。
            return result.model_dump(mode="json")

        # 查询成功后从有限枚举映射出用户可读中文标签。
        status_label = ORDER_STATUS_LABELS[order.status]
        # 构建完整的结构化成功结果。
        result = OrderLookupResult(
            # found=True 表示归属校验已经通过。
            found=True,
            # 返回仓库中的规范订单号。
            order_id=order.order_id,
            # 保留机器可读的有限状态枚举。
            status=order.status,
            # 同时提供面向中文回答的状态标签。
            status_label=status_label,
            # 承运商和物流号可能因未发货而为空。
            carrier=order.carrier,
            tracking_number=order.tracking_number,
            # 使用 ISO 8601 保留时区信息，避免本地时间解释不一致。
            updated_at=order.updated_at.isoformat(),
            # message 提供不经过模型改写也能理解的安全摘要。
            message=f"订单 {order.order_id} 当前状态：{status_label}。",
        )
        # 返回结构化字典，后续模型或图节点可以稳定读取具体字段。
        return result.model_dump(mode="json")

    # 返回已经绑定当前 user_id 和 repository 的 BaseTool 对象。
    return get_order_status
