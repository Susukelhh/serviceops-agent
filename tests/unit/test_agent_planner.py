"""确定性订单规划器、计划 Schema 和工具调用指纹的单元测试。"""

# pytest 提供异步测试标记和异常断言。
import pytest

# ValidationError 验证计划跨字段不变量会在执行前被 Pydantic 拒绝。
from pydantic import ValidationError

# 离线规划器是默认 Agent 决策基线。
from serviceops_agent.agent.planner import DeterministicOrderToolPlanner

# AgentAction/ToolCallPlan/ToolExecutionRecord 构造强类型计划与历史观察。
from serviceops_agent.domain.agent import AgentAction, ToolCallPlan, ToolExecutionRecord

# 指纹函数是请求内重复调用检测的确定性基础。
from serviceops_agent.graph.nodes.order import create_tool_call_fingerprint


def _successful_record(order_id: str, *, fingerprint_character: str) -> ToolExecutionRecord:
    """为规划器单元测试创建一条最小成功观察。"""

    # ToolExecutionRecord 只要求结果非空；规划器不会读取具体订单结果字段。
    return ToolExecutionRecord(
        # 当前唯一允许的工具名。
        tool_name="get_order_status",
        # 历史参数告诉规划器该订单已经调用过。
        arguments={"order_id": order_id},
        # 使用 64 位十六进制字符满足 SHA-256 格式约束。
        fingerprint=fingerprint_character * 64,
        # 表示工具和结果校验均成功。
        succeeded=True,
        # 非空结果满足执行记录不变量。
        result={"found": True},
    )


@pytest.mark.asyncio
async def test_deterministic_planner_queries_each_unique_order_then_finishes() -> None:
    """多订单请求应每轮只调用一个未处理订单，全部观察后明确停止。"""

    # Arrange：创建不访问模型或网络的默认规划器。
    planner = DeterministicOrderToolPlanner()
    # 用户重复提到第一个订单，用于同时验证顺序保持和去重。
    message = "查询 SO100001 和 SO100002，再看一下 SO100001"

    # Act：没有观察历史时应规划第一个唯一订单。
    first_plan = await planner.plan(user_message=message, history=[])
    # Assert：第一轮请求调用白名单工具。
    assert first_plan.action == AgentAction.CALL_TOOL
    # Assert：参数只包含第一个订单号。
    assert first_plan.order_id == "SO100001"
    # Assert：真正 Tool 参数由服务端方法确定性构造。
    assert first_plan.tool_arguments() == {"order_id": "SO100001"}

    # Arrange：把第一条调用结果加入观察历史。
    first_record = _successful_record("SO100001", fingerprint_character="a")
    # Act：第二轮规划必须跳过已经执行的订单。
    second_plan = await planner.plan(user_message=message, history=[first_record])
    # Assert：第二个唯一订单成为下一次工具参数。
    assert second_plan.order_id == "SO100002"

    # Arrange：加入第二条观察，所有用户订单都已处理。
    second_record = _successful_record("SO100002", fingerprint_character="b")
    # Act：规划器观察完整历史后选择停止。
    final_plan = await planner.plan(
        user_message=message,
        history=[first_record, second_record],
    )
    # Assert：不再产生第三次重复工具调用。
    assert final_plan.action == AgentAction.FINISH
    # Assert：非调用动作不能携带任何工具参数。
    assert final_plan.tool_arguments() == {}


@pytest.mark.asyncio
async def test_deterministic_planner_clarifies_without_order_id() -> None:
    """没有合法订单号时必须澄清，不能猜测或遍历用户订单。"""

    # Arrange：创建默认离线规划器。
    planner = DeterministicOrderToolPlanner()
    # Act：问题只有物流意图，没有 SO 加六位数字。
    plan = await planner.plan(user_message="我的物流到哪了", history=[])
    # Assert：规划器明确选择澄清动作。
    assert plan.action == AgentAction.CLARIFY
    # Assert：没有计划任何工具名。
    assert plan.tool_name is None


def test_tool_call_plan_rejects_tool_payload_on_finish() -> None:
    """结构化 Schema 应在图路由前拒绝动作和工具字段互相矛盾的计划。"""

    # Act/Assert：finish 不能夹带一个实际工具调用负载。
    with pytest.raises(ValidationError):
        # Pydantic 会运行 model_validator 并抛出稳定校验异常。
        ToolCallPlan(
            # 声称已经结束。
            action=AgentAction.FINISH,
            # 却同时指定工具，属于语义矛盾。
            tool_name="get_order_status",
            # 夹带的订单号也必须被拒绝。
            order_id="SO100001",
            # 合法长度原因不能掩盖其他字段错误。
            reason="错误的结束计划",
        )


def test_tool_call_fingerprint_is_canonical_and_tool_specific() -> None:
    """参数顺序不应改变指纹，不同工具名必须产生不同指纹。"""

    # Act：同一语义参数使用不同字典插入顺序计算指纹。
    first = create_tool_call_fingerprint(
        "get_order_status",
        {"order_id": "SO100001", "format": "brief"},
    )
    # 第二个字典顺序相反。
    reordered = create_tool_call_fingerprint(
        "get_order_status",
        {"format": "brief", "order_id": "SO100001"},
    )
    # 相同参数但工具名不同，必须位于不同幂等空间。
    other_tool = create_tool_call_fingerprint(
        "another_tool",
        {"order_id": "SO100001", "format": "brief"},
    )

    # Assert：规范 JSON 排序让字典插入顺序不影响摘要。
    assert first == reordered
    # Assert：工具名参与摘要，避免跨工具误判重复调用。
    assert first != other_tool
    # Assert：SHA-256 十六进制摘要固定为 64 位。
    assert len(first) == 64
