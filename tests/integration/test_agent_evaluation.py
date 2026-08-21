"""端到端 Agent 离线评测、失败定位与聚合质量门集成测试。"""

# pytest 让两个完整 LangGraph 评测在异步事件循环内执行。
import pytest

# ValidationError 验证坏标签在运行图或外部模型前被数据 Schema 拒绝。
from pydantic import ValidationError

# Intent 用于故意构造一个错误参考标签，证明质量门真的会失败。
from serviceops_agent.domain.enums import Intent

# 项目评测模型和运行器是本文件的直接测试目标。
from serviceops_agent.evaluation import (
    AgentEvaluationDataset,
    AgentEvaluationThresholds,
    build_offline_agent_evaluation_target,
    evaluate_agent_dataset,
    load_agent_evaluation_dataset,
)


# 完整标准集应该在确定性基线上全部通过，并证明所有退货前置样本零写入。
@pytest.mark.asyncio
async def test_standard_agent_evaluation_dataset_passes_quality_gate() -> None:
    """13 条标准样本应在同一完整 LangGraph 上得到四项 100% 确定性指标。"""

    # Arrange：加载受版本控制的数据集并新建内存索引、Saver 和业务仓库。
    dataset = load_agent_evaluation_dataset(
        "data/evaluation/agent_end_to_end_cases.json"
    )
    graph, return_repository = build_offline_agent_evaluation_target()

    # Act：逐条运行真实节点、条件边、工具循环和 interrupt。
    summary = await evaluate_agent_dataset(graph, return_repository, dataset)

    # Assert：所有聚合门都使用真实样本分母，不能用空集得到虚假满分。
    assert summary.total_cases == 13
    assert summary.passed_cases == 13
    assert summary.overall_pass_rate == 1.0
    assert summary.routing_accuracy == 1.0
    assert summary.tool_trajectory_accuracy == 1.0
    assert summary.response_contract_accuracy == 1.0
    assert summary.safety_invariant_accuracy == 1.0
    assert summary.quality_gate_passed is True
    assert summary.quality_gate_failures == []
    # 报告保存有限真实事件，失败时无需重新调用付费模型才能定位命名或顺序问题。
    first_result = summary.results[0]
    assert first_result.actual_events[:2] == [
        "evaluation:request_received",
        "graph:request_normalized",
    ]
    # FAQ 的三条核心业务事件应按实际执行顺序出现在同一报告中。
    assert "graph:intent_classified_as_faq" in first_result.actual_events
    assert "graph:faq_evidence_retrieved" in first_result.actual_events
    assert "graph:faq_grounded_answer_created" in first_result.actual_events
    # 实际事件只保存代码生成的有限标识，不得复制该 case 的用户问题原文。
    assert all(dataset.cases[0].message not in event for event in first_result.actual_events)
    # 全部样本只运行到普通完成或审批暂停，退货仓库必须保持零记录。
    assert return_repository.count() == 0


# 错误参考标签必须产生可定位的逐样本失败和非通过聚合门。
@pytest.mark.asyncio
async def test_wrong_reference_label_fails_routing_and_overall_gate() -> None:
    """把发票 FAQ 错标为订单意图后，报告应明确指出 intent_mismatch。"""

    # Arrange：读取合法样本，再只修改期望意图来模拟一次人工标注错误或模型回归。
    standard_dataset = load_agent_evaluation_dataset(
        "data/evaluation/agent_end_to_end_cases.json"
    )
    wrong_case = standard_dataset.cases[0].model_copy(
        update={"expected_intent": Intent.ORDER_STATUS}
    )
    failing_dataset = AgentEvaluationDataset(
        dataset_id="intent-regression-test",
        version="1.0.0",
        description="故意错误的参考标签，用于验证质量门不会虚假通过。",
        thresholds=AgentEvaluationThresholds(),
        cases=[wrong_case],
    )
    graph, return_repository = build_offline_agent_evaluation_target()

    # Act：实际图仍会正确识别 FAQ，因此它应与错误标签不一致。
    summary = await evaluate_agent_dataset(
        graph,
        return_repository,
        failing_dataset,
    )

    # Assert：只失败的路由维度会给出稳定规则码，其他真实行为仍可独立通过。
    assert summary.quality_gate_passed is False
    assert summary.routing_accuracy == 0.0
    assert summary.overall_pass_rate == 0.0
    assert summary.tool_trajectory_accuracy == 1.0
    assert summary.results[0].violations == ["intent_mismatch"]
    assert summary.quality_gate_failures == [
        "overall_pass_rate_below_threshold",
        "routing_accuracy_below_threshold",
    ]


def test_dataset_rejects_duplicate_case_ids_before_execution() -> None:
    """重复 case_id 会破坏失败定位，应在执行整图前由 Pydantic 拒绝。"""

    # Arrange：复用一条合法样本两次，其他数据集字段保持有效。
    standard_dataset = load_agent_evaluation_dataset(
        "data/evaluation/agent_end_to_end_cases.json"
    )
    duplicated_case = standard_dataset.cases[0]

    # Act/Assert：跨样本 model_validator 必须发现重复 ID。
    with pytest.raises(ValidationError, match="case_id 不能重复"):
        AgentEvaluationDataset.model_validate(
            {
                "dataset_id": "duplicate-case-test",
                "version": "1.0.0",
                "description": "验证重复样本标识会被拒绝。",
                "thresholds": AgentEvaluationThresholds().model_dump(),
                "cases": [
                    duplicated_case.model_dump(mode="json"),
                    duplicated_case.model_dump(mode="json"),
                ],
            }
        )
