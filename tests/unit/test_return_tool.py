"""退货写工具的身份绑定、资格检查和幂等行为单元测试。"""

# ReturnRequestResult 把普通工具字典重新校验成强类型结果，减少重复取键。
from serviceops_agent.domain.returns import ReturnRequestResult

# 默认订单仓库提供本人已签收、本人运输中和他人订单三种固定测试数据。
from serviceops_agent.infrastructure.order_repository import default_order_repository

# 每个测试创建独立进程内退货仓库，避免测试间共享写记录。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
)

# 工具工厂把可信 user_id 绑定在闭包中，调用参数不允许提交身份。
from serviceops_agent.tools.return_tools import create_return_request_tool


def test_return_tool_binds_identity_outside_tool_schema() -> None:
    """模型或审批恢复值不应能在工具参数中伪造 user_id。"""

    # Arrange：创建只属于当前测试的空退货仓库。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    # user-001 由可信执行节点绑定，而不是由工具输入提供。
    write_tool = create_return_request_tool(user_id="user-001", repository=repository)

    # Act：读取 LangChain 工具公开给调用方的 JSON Schema 字段集合。
    schema_properties = write_tool.args_schema.model_json_schema()["properties"]

    # Assert：调用方只能提交审批过的订单号、原因和幂等键。
    assert set(schema_properties) == {"order_id", "reason", "idempotency_key"}
    # Assert：敏感身份字段绝不能出现在工具参数 Schema 中。
    assert "user_id" not in schema_properties


def test_return_tool_creates_once_and_replays_same_idempotency_key() -> None:
    """相同幂等键和相同负载重试时必须返回原编号且不重复写入。"""

    # Arrange：创建独立仓库和绑定 user-001 的写工具。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    write_tool = create_return_request_tool(user_id="user-001", repository=repository)
    # 同一请求的三个业务参数会在两次工具调用中保持完全一致。
    arguments = {
        "order_id": "SO100002",
        "reason": "商品尺寸不合适",
        "idempotency_key": "unit-idempotent-001",
    }

    # Act：第一次调用应创建记录。
    first = ReturnRequestResult.model_validate(write_tool.invoke(arguments))
    # Act：模拟 HTTP 重试或恢复重放，再执行完全相同的调用。
    second = ReturnRequestResult.model_validate(write_tool.invoke(arguments))

    # Assert：第一次明确表示新建成功。
    assert first.success is True
    assert first.created is True
    assert first.idempotent_replay is False
    # Assert：第二次明确表示幂等命中，而不是再次创建。
    assert second.success is True
    assert second.created is False
    assert second.idempotent_replay is True
    # Assert：两次响应必须返回同一个稳定业务编号。
    assert second.return_request_id == first.return_request_id
    # Assert：仓库中始终只有一条唯一申请。
    assert repository.count() == 1


def test_return_tool_rejects_idempotency_key_reused_for_different_payload() -> None:
    """同一幂等键用于不同申请内容时必须冲突且保留原记录。"""

    # Arrange：创建独立仓库和身份绑定工具。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    write_tool = create_return_request_tool(user_id="user-001", repository=repository)
    # 首次调用占用该幂等键。
    write_tool.invoke(
        {
            "order_id": "SO100002",
            "reason": "商品尺寸不合适",
            "idempotency_key": "unit-conflict-001",
        }
    )

    # Act：第二次故意复用同一键但更改原因。
    conflict = ReturnRequestResult.model_validate(
        write_tool.invoke(
            {
                "order_id": "SO100002",
                "reason": "商品颜色与预期不符",
                "idempotency_key": "unit-conflict-001",
            }
        )
    )

    # Assert：冲突不是成功重放，也不能返回旧申请编号冒充本次成功。
    assert conflict.success is False
    assert conflict.failure_code == "idempotency_conflict"
    assert conflict.return_request_id is None
    # Assert：原记录没有被覆盖或新增。
    assert repository.count() == 1


def test_return_tool_hides_unauthorized_order_and_rejects_ineligible_order() -> None:
    """写边界应同时执行订单归属和已签收资格检查。"""

    # Arrange：user-001 的工具不能操作 user-002 的 SO200001。
    repository = InMemoryReturnRequestRepository(default_order_repository)
    write_tool = create_return_request_tool(user_id="user-001", repository=repository)

    # Act：尝试为其他用户订单创建申请。
    unauthorized = ReturnRequestResult.model_validate(
        write_tool.invoke(
            {
                "order_id": "SO200001",
                "reason": "测试其他用户订单权限",
                "idempotency_key": "unit-authz-001",
            }
        )
    )
    # Act：尝试为本人尚未签收的 SO100001 创建申请。
    ineligible = ReturnRequestResult.model_validate(
        write_tool.invoke(
            {
                "order_id": "SO100001",
                "reason": "运输中的订单申请退货",
                "idempotency_key": "unit-status-001",
            }
        )
    )

    # Assert：越权和不存在共享同一有限错误码，避免订单枚举。
    assert unauthorized.failure_code == "order_unavailable"
    # Assert：本人订单状态不符合要求时返回不同的业务资格码。
    assert ineligible.failure_code == "order_not_eligible"
    # Assert：两次拒绝都没有写入申请。
    assert repository.count() == 0
