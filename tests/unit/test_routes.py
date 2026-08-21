"""条件边路由函数的单元测试。"""

# pytest 提供参数化能力，减少为每个意图重复编写结构相同的测试函数。
import pytest

# Intent 用于构造路由函数可能接收到的四种有限分类结果。
from serviceops_agent.domain.enums import Intent

# 三个纯路由函数分别处理业务意图、FAQ 证据门和最终答案安全门。
from serviceops_agent.graph.routes import (
    select_faq_answer_path,
    select_faq_evidence_path,
    select_order_execution_path,
    select_order_plan_path,
    select_response_path,
)


# 为四个合法意图分别生成测试，确保枚举到路由键的映射没有遗漏。
@pytest.mark.parametrize(
    # intent 是输入状态字段，expected_path 是预期返回的路由键。
    ("intent", "expected_path"),
    # 四组数据必须与 builder.py 中的条件边映射保持一致。
    [
        # FAQ 意图映射到 faq 路由键。
        (Intent.FAQ, "faq"),
        # 订单状态意图映射到 order 路由键。
        (Intent.ORDER_STATUS, "order"),
        # 退货申请映射到先准备草案的 return_request 路由键。
        (Intent.RETURN_REQUEST, "return_request"),
        # 人工接管意图映射到 human 路由键。
        (Intent.HUMAN_HANDOFF, "human"),
    ],
)
def test_select_response_path(intent: Intent, expected_path: str) -> None:
    """每个有限意图都应映射到唯一的下一跳。"""

    # Act/Assert：把当前意图写入最小状态，并验证函数返回唯一正确的下一跳。
    assert select_response_path({"intent": intent}) == expected_path


def test_select_response_path_defaults_to_human() -> None:
    """缺失意图时不能猜测，必须采用安全默认路径。"""

    # 缺失 intent 模拟上游异常；安全默认值必须是 human，而不能猜测自动路径。
    assert select_response_path({}) == "human"


def test_select_faq_evidence_path_requires_explicit_evidence() -> None:
    """只有证据门和命中列表同时有效时才允许进入 FAQ 回答节点。"""

    # 使用 object 作为占位命中即可验证纯路由逻辑，不需要构造真实高维向量。
    placeholder_hit = object()
    # 显式 True 且命中非空时进入 answer。
    assert (
        select_faq_evidence_path(
            {
                "has_sufficient_evidence": True,
                # 路由函数只检查列表非空，不读取命中内部字段。
                "retrieval_hits": [placeholder_hit],  # type: ignore[list-item]
            }
        )
        == "answer"
    )


@pytest.mark.parametrize(
    # state 覆盖缺字段、显式无证据和标志/命中不一致三种异常输入。
    "state",
    [
        # 上游节点未写任何证据字段。
        {},
        # 明确没有充分证据。
        {"has_sufficient_evidence": False, "retrieval_hits": []},
        # 即使标志为 True，空命中列表也不能生成回答。
        {"has_sufficient_evidence": True, "retrieval_hits": []},
    ],
)
def test_select_faq_evidence_path_defaults_to_human(state: dict[str, object]) -> None:
    """证据状态缺失或矛盾时必须采用人工安全默认路径。"""

    # 类型忽略只针对参数化普通字典；运行时正是要验证异常 State 输入。
    assert select_faq_evidence_path(state) == "human"  # type: ignore[arg-type]


def test_select_faq_answer_path_requires_grounded_answer_and_citation() -> None:
    """第三道安全门只有在标记、答案和引用同时有效时才允许结束。"""

    # Citation 的内部字段不属于纯路由测试目标，用 object 占位即可验证非空条件。
    placeholder_citation = object()
    # 三项条件同时存在时返回 complete，图构建器会把它映射到 END。
    assert (
        select_faq_answer_path(
            {
                # 生成节点已经明确通过引用白名单。
                "faq_answer_grounded": True,
                # 最终答案非空。
                "answer": "基于知识证据的回答",
                # 至少存在一条最终引用。
                "citations": [placeholder_citation],  # type: ignore[list-item]
            }
        )
        == "complete"
    )


@pytest.mark.parametrize(
    # state 覆盖缺失 grounding、空答案和空引用三种不能安全返回的状态。
    "state",
    [
        # 所有字段都缺失时必须人工。
        {},
        # 即使存在答案和引用，未通过白名单的标记也不能放行。
        {"faq_answer_grounded": False, "answer": "未验证答案", "citations": [object()]},
        # 已通过标记但答案为空，说明生成结果不完整。
        {"faq_answer_grounded": True, "answer": "", "citations": [object()]},
        # 已通过标记但没有引用，不满足 grounded answer 定义。
        {"faq_answer_grounded": True, "answer": "没有来源", "citations": []},
    ],
)
def test_select_faq_answer_path_defaults_to_human(state: dict[str, object]) -> None:
    """最终回答状态缺失或矛盾时必须采用人工安全默认路径。"""

    # 参数化字典故意包含运行时异常组合，因此只在静态类型层忽略 TypedDict 差异。
    assert select_faq_answer_path(state) == "human"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    # state/expected_path 覆盖订单规划器的三个正常动作出口。
    ("state", "expected_path"),
    [
        # call_tool 同时有计划对象时进入工具执行器；object 只用于纯路由非空检查。
        ({"agent_next_action": "call_tool", "planned_tool_call": object()}, "execute"),
        # finish 且存在观察历史时进入确定性汇总。
        ({"agent_next_action": "finish", "tool_execution_records": [object()]}, "finalize"),
        # clarify 进入参数追问节点。
        ({"agent_next_action": "clarify"}, "clarify"),
    ],
)
def test_select_order_plan_path_maps_valid_actions(
    state: dict[str, object],
    expected_path: str,
) -> None:
    """订单规划动作应映射到唯一下一跳。"""

    # 类型忽略只针对最小占位对象；路由函数不读取计划和观察内部字段。
    assert select_order_plan_path(state) == expected_path  # type: ignore[arg-type]


@pytest.mark.parametrize(
    # 缺动作、空计划和无观察 finish 都必须采用人工安全默认路径。
    "state",
    [
        # 完全缺失规划字段。
        {},
        # call_tool 没有具体计划。
        {"agent_next_action": "call_tool", "planned_tool_call": None},
        # finish 没有任何可汇总观察。
        {"agent_next_action": "finish", "tool_execution_records": []},
        # 未知字符串不能被当成新的图节点名。
        {"agent_next_action": "invented_action"},
    ],
)
def test_select_order_plan_path_defaults_to_human(state: dict[str, object]) -> None:
    """订单计划状态缺失或矛盾时必须转人工。"""

    # 故意构造异常 State，因此只忽略 TypedDict 静态差异。
    assert select_order_plan_path(state) == "human"  # type: ignore[arg-type]


def test_select_order_execution_path_only_continues_after_explicit_success() -> None:
    """工具执行回边必须同时满足显式成功和 continue 动作。"""

    # 两项条件同时存在时允许回到规划器。
    assert (
        select_order_execution_path(
            {"agent_execution_succeeded": True, "agent_next_action": "continue"}
        )
        == "continue"
    )
    # 缺失成功标记时不能形成循环。
    assert select_order_execution_path({"agent_next_action": "continue"}) == "human"
    # 执行失败即使动作字段错误写成 continue 也不能绕过。
    assert (
        select_order_execution_path(
            {"agent_execution_succeeded": False, "agent_next_action": "continue"}
        )
        == "human"
    )
