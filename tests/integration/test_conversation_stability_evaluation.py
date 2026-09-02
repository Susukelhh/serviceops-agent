"""第48步多轮稳定性数据集、指标与失败门集成测试。"""

import pytest
from pydantic import ValidationError

from serviceops_agent.evaluation import (
    ConversationStabilityDataset,
    evaluate_conversation_stability,
    load_conversation_stability_dataset,
)


def test_standard_conversation_stability_dataset_passes_all_gates() -> None:
    """18轮确定性场景必须同时通过解析、记忆、执行和隔离四项门。"""

    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    summary = evaluate_conversation_stability(dataset)

    assert len(dataset.scenarios) == 6
    assert summary.total_turns == 18
    assert summary.passed_turns == 18
    assert summary.overall_pass_rate == 1.0
    assert summary.resolution_accuracy == 1.0
    assert summary.memory_accuracy == 1.0
    assert summary.execution_safety_accuracy == 1.0
    assert summary.isolation_accuracy == 1.0
    assert summary.quality_gate_passed is True
    assert summary.quality_gate_failures == []


def test_wrong_resolution_gold_fails_only_relevant_aggregate_gate() -> None:
    """故意破坏一个独立问题金标时，报告应给出可定位规则码和非零失败门。"""

    standard = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    first_scenario = standard.scenarios[0]
    wrong_first = first_scenario.turns[0].model_copy(
        update={"expected_standalone_question": "故意错误的独立问题"}
    )
    failing = standard.model_copy(
        update={
            "dataset_id": "conversation-stability-negative-control",
            "scenarios": [
                first_scenario.model_copy(
                    update={"turns": [wrong_first, first_scenario.turns[1]]}
                )
            ],
        }
    )

    summary = evaluate_conversation_stability(failing)

    assert summary.overall_pass_rate == 0.5
    assert summary.resolution_accuracy == 0.5
    assert summary.memory_accuracy == 1.0
    assert summary.execution_safety_accuracy == 1.0
    assert summary.isolation_accuracy == 1.0
    assert summary.results[0].failure_codes == ["resolution_mismatch"]
    assert summary.quality_gate_passed is False
    assert summary.quality_gate_failures == [
        "overall_pass_rate_below_threshold",
        "resolution_accuracy_below_threshold",
    ]


def test_report_omits_messages_answers_tokens_and_memory_contents() -> None:
    """CI产物只保留场景ID、序号、比例和规则码，不复制对话正文。"""

    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    serialized = evaluate_conversation_stability(dataset).model_dump_json()

    for forbidden in (
        dataset.scenarios[0].turns[0].message,
        "OFFLINE_ASSISTANT_SENTINEL",
        "claim_token",
        "SO100001",
        "KB-INVOICE-001",
        "secret-marker-001",
    ):
        assert forbidden not in serialized


def test_dataset_rejects_duplicate_scenario_ids_before_evaluation() -> None:
    """重复场景ID会破坏失败定位，必须在状态变更前拒绝。"""

    standard = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    with pytest.raises(ValidationError, match="scenario_id 不能重复"):
        ConversationStabilityDataset.model_validate(
            {
                **standard.model_dump(mode="json"),
                "scenarios": [
                    standard.scenarios[0].model_dump(mode="json"),
                    standard.scenarios[0].model_dump(mode="json"),
                ],
            }
        )
