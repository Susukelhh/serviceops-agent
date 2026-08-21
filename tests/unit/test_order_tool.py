"""身份绑定只读订单工具的单元测试。"""

# PROJECT_ROOT/resolve_project_path 用于约束订单种子必须遵守统一的部署路径策略。
from serviceops_agent.config.paths import PROJECT_ROOT, resolve_project_path

# 默认路径和默认仓库共同覆盖启动加载行为，避免 wheel 安装后从 site-packages 反推数据目录。
from serviceops_agent.infrastructure.order_repository import (
    DEFAULT_ORDER_DATA_PATH,
    default_order_repository,
)

# create_order_status_tool 为不同登录用户创建参数 Schema 相同但权限上下文不同的工具。
from serviceops_agent.tools.order_tools import create_order_status_tool


def test_default_order_data_path_is_anchored_to_project_root() -> None:
    """默认订单种子必须使用统一项目根目录，不能依赖模块安装位置。"""

    # Arrange：通过公共解析函数得到部署环境期望的订单种子绝对路径。
    expected_path = resolve_project_path("data/seed/orders.json")
    # Assert：仓库默认值必须与统一解析结果完全一致。
    assert expected_path == DEFAULT_ORDER_DATA_PATH
    # Assert：源码测试环境中结果应明确位于项目根目录的 data/seed 下。
    assert expected_path == (PROJECT_ROOT / "data/seed/orders.json").resolve()


def test_order_tool_schema_does_not_expose_user_id() -> None:
    """模型只能填写订单号，不能填写或伪造用户身份。"""

    # Arrange：系统为已认证用户 user-001 创建工具。
    order_tool = create_order_status_tool("user-001", default_order_repository)

    # Assert：模型看到的 JSON Schema 只有 order_id 参数。
    assert set(order_tool.args) == {"order_id"}


def test_order_tool_returns_owned_order() -> None:
    """归属当前用户的订单应返回结构化状态和物流信息。"""

    # Arrange：创建绑定 user-001 身份的只读工具。
    order_tool = create_order_status_tool("user-001", default_order_repository)
    # Act：使用小写订单前缀，顺便验证 Pydantic 规范化逻辑。
    result = order_tool.invoke({"order_id": "so100001"})

    # Assert：订单属于 user-001，因此查询成功。
    assert result["found"] is True
    # Assert：工具把订单号规范化为大写格式。
    assert result["order_id"] == "SO100001"
    # Assert：返回有限机器状态，便于图节点稳定判断。
    assert result["status"] == "shipped"
    # Assert：物流单号来自仓库数据而非模型生成。
    assert result["tracking_number"] == "SF1234567890"


def test_order_tool_hides_other_users_order() -> None:
    """查询其他用户订单时不能返回任何订单事实。"""

    # Arrange：工具仍绑定 user-001，但 SO200001 实际属于 user-002。
    order_tool = create_order_status_tool("user-001", default_order_repository)
    # Act：尝试查询不属于当前用户的订单。
    forbidden_result = order_tool.invoke({"order_id": "SO200001"})
    # Act：同时查询一个真正不存在的订单，用于比较两个响应是否一致。
    missing_result = order_tool.invoke({"order_id": "SO999999"})

    # Assert：越权查询不能成功。
    assert forbidden_result["found"] is False
    # Assert：失败结果不能包含其他用户订单状态。
    assert forbidden_result["status"] is None
    # Assert：越权和不存在使用同一文案，防止攻击者枚举有效订单号。
    assert forbidden_result["message"] == missing_result["message"]
