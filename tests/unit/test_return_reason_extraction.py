"""退货原因抽取的明确表达、口语因果和安全澄清边界测试。"""

# pytest 用参数表覆盖允许和拒绝的自然语言结构。
import pytest

# extract_explicit_return_reason 是草案节点唯一使用的确定性原因提取入口。
from serviceops_agent.graph.nodes.returns import extract_explicit_return_reason


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    [
        # 旧版“原因：”格式必须保持完全兼容。
        ("为 SO100002 申请退货，原因：商品尺寸不合适", "商品尺寸不合适"),
        # 新版允许带“但”的明确口语因果，只截取下一个逗号之前的原话。
        (
            "SO100002 已经收到了，但尺寸不合适，麻烦替我发起退货",
            "尺寸不合适",
        ),
        # “因为”同样明确表达因果关系。
        ("我想退货，因为商品颜色与页面不一致。", "商品颜色与页面不一致"),
        # 只有退货动作但没有任何原因标记时仍应追问，不能凭空猜测。
        ("帮我给 SO100002 发起退货", None),
    ],
)
def test_extract_explicit_return_reason(
    message: str,
    expected_reason: str | None,
) -> None:
    """只接受用户明确说出的原因，并保持原文边界。"""

    assert extract_explicit_return_reason(message) == expected_reason
