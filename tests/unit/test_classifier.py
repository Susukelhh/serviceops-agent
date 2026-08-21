"""意图分类节点的单元测试。"""

# pytest 提供参数化能力，让同一个测试函数覆盖多个代表性输入。
import pytest

# Intent 用于构造预期的有限枚举结果。
from serviceops_agent.domain.enums import Intent

# classify_intent 是本文件直接隔离测试的目标函数。
from serviceops_agent.graph.nodes.classifier import classify_intent


# 参数化装饰器会为下面三组“输入文本—期望意图”分别生成一个独立测试用例。
@pytest.mark.parametrize(
    # 两个参数名会按顺序注入测试函数的 message 和 expected_intent。
    ("message", "expected_intent"),
    # 覆盖 FAQ、只读订单、退货写操作和未知问题四种主要分类分支。
    [
        # 发票问题应被识别为知识型 FAQ。
        ("电子发票怎么申请", Intent.FAQ),
        # 包含订单关键词的问题应进入订单状态路径。
        ("我的订单到哪里了", Intent.ORDER_STATUS),
        # 退货申请关键词必须优先进入需要人工审批的写操作路径。
        ("我要为订单 SO100002 申请退货，原因：商品不合适", Intent.RETURN_REQUEST),
        # 不含已知关键词的问题应采用安全的人工接管兜底。
        ("我有一个非常特殊的问题", Intent.HUMAN_HANDOFF),
    ],
)
def test_classify_intent_uses_safe_fallback(message: str, expected_intent: Intent) -> None:
    """已知问题进入对应分支，未知问题必须安全地转人工。"""

    # Arrange/Act：构造分类节点所需的最小状态，并直接调用节点函数。
    result = classify_intent({"normalized_message": message})

    # Assert：节点写入的意图必须等于当前参数组的期望枚举。
    assert result["intent"] == expected_intent
    # Assert：基线命中关键词时为 1，未知安全兜底时为 0。
    assert result["intent_confidence"] in {0.0, 1.0}
    # Assert：事件必须使用列表，才能被 ServiceState 的 add Reducer 正确累加。
    assert isinstance(result["events"], list)
