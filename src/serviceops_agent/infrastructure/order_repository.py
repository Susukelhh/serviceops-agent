"""订单仓库接口及其第一版 JSON 模拟实现。

图节点依赖 OrderRepository 协议而不是直接读取 JSON。未来替换为 PostgreSQL 或企业订单
HTTP 服务时，只需增加新的仓库实现，不需要重写 LangGraph 业务控制流。
"""

# json 负责解析第一阶段的本地模拟订单数据。
import json

# Sequence 表示仓库初始化可以接收列表、元组等只读订单序列。
from collections.abc import Sequence

# Path 提供跨平台路径拼接和 UTF-8 文件读取。
from pathlib import Path

# Protocol 定义仓库能力接口，让节点可依赖抽象并在测试中注入替身。
from typing import Protocol

# resolve_project_path 统一处理源码仓库、wheel 安装和容器挂载三种运行形态的数据路径。
from serviceops_agent.config.paths import resolve_project_path

# OrderRecord 对每条外部数据执行格式、枚举和时间字段校验。
from serviceops_agent.domain.orders import OrderRecord

# 相对路径必须锚定显式项目根目录；不能从 site-packages 位置反推容器业务数据目录。
DEFAULT_ORDER_DATA_PATH = resolve_project_path("data/seed/orders.json")


class OrderRepository(Protocol):
    """订单读取能力的最小接口。"""

    def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None:
        """只返回属于指定用户的订单；不存在或不属于该用户都返回 None。"""


class InMemoryOrderRepository:
    """把经过校验的订单加载到内存字典中的第一版仓库。"""

    def __init__(self, orders: Sequence[OrderRecord]) -> None:
        """使用订单号建立只读查询索引。"""

        # 字典键是唯一订单号，值是完整记录；查询复杂度由线性扫描降为近似 O(1)。
        self._orders = {order.order_id: order for order in orders}

    @classmethod
    def from_json(cls, path: Path) -> "InMemoryOrderRepository":
        """从 UTF-8 JSON 文件加载并校验模拟订单。"""

        # read_text 明确指定 UTF-8，避免 Windows 默认编码导致中文承运商名称损坏。
        raw_text = path.read_text(encoding="utf-8")
        # json.loads 把文本转换为 Python 对象，后续再交给 Pydantic 做业务字段校验。
        raw_orders = json.loads(raw_text)
        # 种子文件顶层必须是数组；对象或字符串通常表示数据文件结构写错。
        if not isinstance(raw_orders, list):
            # 在应用启动阶段尽早失败，比运行到工具节点才返回模糊错误更容易排查。
            raise ValueError(f"订单种子文件必须是 JSON 数组：{path}")
        # 每条字典都经过 OrderRecord.model_validate，非法订单号或状态会立即报错。
        orders = [OrderRecord.model_validate(item) for item in raw_orders]
        # 使用校验后的领域对象创建仓库实例。
        return cls(orders)

    def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None:
        """按订单号查询，并在仓库边界强制检查订单归属。"""

        # 先通过索引查找订单；找不到时 record 为 None。
        record = self._orders.get(order_id)
        # 对“不存在”和“不属于当前用户”返回同一个结果，避免泄露其他用户订单是否存在。
        if record is None or record.user_id != user_id:
            # None 由工具层转换为统一、安全的用户提示。
            return None
        # 只有归属校验通过后才把完整订单记录返回给上层。
        return record


# 应用启动时加载一次模拟数据，避免每次工具调用都重新读取和解析 JSON 文件。
default_order_repository = InMemoryOrderRepository.from_json(DEFAULT_ORDER_DATA_PATH)
