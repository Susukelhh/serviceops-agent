"""结构化 LLM 分类节点的无网络单元测试。"""

# pytest 提供异步测试标记。
import pytest

# IntentClassification 构造已经过 Pydantic 校验的模型返回值。
from serviceops_agent.domain.classification import IntentClassification

# Intent 用于设置模型原始意图并断言安全门后的最终意图。
from serviceops_agent.domain.enums import Intent

# 稳定内部异常用于模拟真实 SDK 已经被适配器归一化后的模型故障。
from serviceops_agent.llm.errors import LLMFailureKind, LLMServiceError

# 系统提示与工厂函数分别用于验证敏感政策边界和 LangGraph 节点后处理逻辑。
from serviceops_agent.llm.intent_classifier import (
    CLASSIFIER_SYSTEM_PROMPT,
    create_llm_intent_classifier_node,
)


class StubClassificationClient:
    """返回固定结构化结果的模型替身，不访问网络也不消耗 Token。"""

    def __init__(self, result: IntentClassification) -> None:
        """保存当前测试希望模型返回的结果。"""

        # result 已由 Pydantic 校验，classify 只需原样返回。
        self._result = result

    async def classify(self, message: str) -> IntentClassification:
        """忽略文本内容并返回预设结果，隔离测试节点的后处理逻辑。"""

        # 显式引用 message，说明测试替身收到真实节点传入的文本但不做外部推理。
        _ = message
        # 返回固定结果，让测试完全可重复。
        return self._result


class FailingClassificationClient:
    """始终抛出脱敏模型异常的测试替身，用于验证图内安全降级。"""

    def __init__(self, error: LLMServiceError) -> None:
        """保存测试希望节点处理的故障类别。"""

        # error 已经是适配器层归一化后的异常，不包含真实密钥或服务商响应正文。
        self._error = error

    async def classify(self, message: str) -> IntentClassification:
        """模拟远程模型调用失败，不执行任何真实网络请求。"""

        # 显式引用 message，表明替身保持与真实客户端一致的方法签名。
        _ = message
        # 抛出预设故障，让被测节点决定是否安全转人工。
        raise self._error


# 该测试需要 await 异步 LangGraph 节点闭包。
@pytest.mark.asyncio
async def test_llm_classifier_accepts_high_confidence_result() -> None:
    """高置信度有限意图应被系统接受。"""

    # Arrange：构造一个高于 0.65 阈值的订单分类结果。
    client = StubClassificationClient(
        IntentClassification(
            # 模拟模型选择订单状态路径。
            intent=Intent.ORDER_STATUS,
            # 0.92 高于下面节点配置的安全阈值。
            confidence=0.92,
            # reason 是简短可审计依据。
            reason="用户询问订单物流进度",
        )
    )
    # 创建已经注入测试客户端和阈值的异步节点。
    node = create_llm_intent_classifier_node(client=client, confidence_threshold=0.65)

    # Act：使用最小 ServiceState 调用节点。
    result = await node({"normalized_message": "查询订单 SO100001"})

    # Assert：高置信度订单意图不应被安全门覆盖。
    assert result["intent"] == Intent.ORDER_STATUS
    # Assert：保留原始置信度供后续评测和阈值分析。
    assert result["intent_confidence"] == 0.92
    # Assert：订单分类不需要直接转人工。
    assert result["requires_human"] is False
    # Assert：第一条业务事件必须与确定性分类器共享同一后端无关契约。
    assert result["events"] == [
        "graph:intent_classified_as_order_status",
        "diagnostic:intent_classifier_backend_llm",
    ]


# 参数化覆盖所有有限意图，防止以后只修某一个标签又让其他候选轨迹与基线分叉。
@pytest.mark.parametrize(
    ("intent", "expected_business_event"),
    [
        (Intent.FAQ, "graph:intent_classified_as_faq"),
        (Intent.ORDER_STATUS, "graph:intent_classified_as_order_status"),
        (Intent.RETURN_REQUEST, "graph:intent_classified_as_return_request"),
        (Intent.HUMAN_HANDOFF, "graph:intent_classified_as_human_handoff"),
    ],
)
@pytest.mark.asyncio
async def test_llm_classifier_uses_backend_neutral_business_event_for_every_intent(
    intent: Intent,
    expected_business_event: str,
) -> None:
    """四种 LLM 结果都必须先记录与离线基线相同的业务事件。"""

    # Arrange：每次只替换结构化意图，置信度固定高于生产阈值。
    client = StubClassificationClient(
        IntentClassification(
            intent=intent,
            confidence=0.95,
            reason="无网络测试中的固定结构化分类结果",
        )
    )
    # 创建与候选实验相同安全阈值的分类节点。
    node = create_llm_intent_classifier_node(client=client, confidence_threshold=0.65)

    # Act：用户文本不影响固定替身，但仍经过真实节点后处理和事件生成。
    result = await node({"normalized_message": "测试分类事件契约"})

    # Assert：业务事件位于首位，后端信息只能作为独立诊断事件出现。
    assert result["events"] == [
        expected_business_event,
        "diagnostic:intent_classifier_backend_llm",
    ]


# 第二个异步测试验证低置信度安全门。
@pytest.mark.asyncio
async def test_llm_classifier_routes_low_confidence_result_to_human() -> None:
    """即使模型给出自动意图，置信度不足也必须转人工。"""

    # Arrange：模型输出 FAQ，但置信度只有 0.40。
    client = StubClassificationClient(
        IntentClassification(
            # 原始模型标签是 FAQ。
            intent=Intent.FAQ,
            # 0.40 低于系统 0.65 阈值。
            confidence=0.40,
            # 低把握原因仍被保存用于错误分析。
            reason="文本含义不明确",
        )
    )
    # 创建与生产相同阈值的节点。
    node = create_llm_intent_classifier_node(client=client, confidence_threshold=0.65)

    # Act：调用异步分类节点。
    result = await node({"normalized_message": "帮我看看"})

    # Assert：系统接受的最终意图必须被覆盖为人工接管。
    assert result["intent"] == Intent.HUMAN_HANDOFF
    # Assert：低置信度覆盖后必须明确要求人工介入。
    assert result["requires_human"] is True
    # Assert：事件名必须描述最终接受的安全意图，而非原始 FAQ 标签。
    assert result["events"] == [
        "graph:intent_classified_as_human_handoff",
        "diagnostic:intent_classifier_backend_llm",
    ]


# 第三个异步测试验证外部模型故障不会继续向 FastAPI 传播。
@pytest.mark.asyncio
async def test_llm_classifier_falls_back_to_human_on_model_failure() -> None:
    """模型超时时应返回可路由状态，而不是让状态图抛出异常。"""

    # Arrange：创建一个标记为可稍后重试的超时错误。
    failure = LLMServiceError(LLMFailureKind.TIMEOUT, retryable=True)
    # 注入始终失败的无网络客户端，保证测试不消耗任何模型额度。
    client = FailingClassificationClient(failure)
    # 创建与生产路径相同的异步分类节点。
    node = create_llm_intent_classifier_node(client=client, confidence_threshold=0.65)

    # Act：执行节点；如果安全降级无效，这里会直接抛出 failure 导致测试失败。
    result = await node(
        {
            # 固定请求 ID 用于验证日志路径能够读取链路标识。
            "request_id": "test-llm-timeout",
            # 规范化文本模拟前一个 LangGraph 节点已经完成输入清洗。
            "normalized_message": "查询订单 SO100001",
        }
    )

    # Assert：模型失败时采用安全默认人工意图，绝不猜测订单或 FAQ 路由。
    assert result["intent"] == Intent.HUMAN_HANDOFF
    # Assert：没有得到有效模型结果，因此置信度必须明确归零。
    assert result["intent_confidence"] == 0.0
    # Assert：调用方应根据该字段创建人工客服任务。
    assert result["requires_human"] is True
    # Assert：内部状态保留有限错误码，便于后续监控聚合。
    assert result["llm_failure_code"] == "timeout"
    # Assert：轨迹记录脱敏类别，不包含 SDK 响应正文。
    assert result["events"] == [
        "graph:intent_classified_as_human_handoff",
        "diagnostic:llm_timeout_fallback_to_human",
    ]


def test_classifier_prompt_routes_non_public_internal_policy_to_human() -> None:
    """分类提示必须明确约束非公开政策，不能依赖模型自行猜测公开范围。"""

    # Assert：三个关键边界词共同覆盖本次真实失败样本及相邻的审批/风控问题。
    assert "非公开内部政策" in CLASSIFIER_SYSTEM_PROMPT
    assert "天气、投资、医疗、写作" in CLASSIFIER_SYSTEM_PROMPT
    assert "也必须选择human_handoff" in CLASSIFIER_SYSTEM_PROMPT


def test_promoted_prompt_fingerprint_matches_frozen_experiment() -> None:
    """生产提示一旦意外改字，锁定实验的可复现指纹就必须立即报警。"""

    # hashlib属于Python标准库，这里只对公开提示做指纹，不涉及API Key。
    import hashlib

    # 使用与实验脚本完全相同的UTF-8编码和SHA-256算法。
    prompt_hash = hashlib.sha256(CLASSIFIER_SYSTEM_PROMPT.encode("utf-8")).hexdigest()

    # 该指纹对应通过32条开发题和16条一次性锁定题的生产提示。
    assert prompt_hash == "ec3523ede957c5718139be06989a6ce41902eef6aaf1085e4ae537dfb2e51328"
