"""第49步真实候选多轮实验的零费用聚合与预算测试。"""

from datetime import UTC, datetime

import pytest

from serviceops_agent.domain.enums import Intent
from serviceops_agent.evaluation import (
    QwenMultiTurnResult,
    QwenMultiTurnTrialSummary,
    build_offline_agent_evaluation_target,
    enforce_qwen_multi_turn_budget,
    estimate_qwen_multi_turn_chat_calls,
    load_conversation_stability_dataset,
    load_qwen_multi_turn_config,
    override_qwen_multi_turn_trials,
    run_qwen_multi_turn_experiment,
    summarize_qwen_multi_turn_experiment,
)


def _trial(
    trial_number: int,
    scenario_turn_counts: dict[str, int],
    *,
    failed_scenario: str | None = None,
) -> QwenMultiTurnTrialSummary:
    results: list[QwenMultiTurnResult] = []
    for scenario_id, turn_count in scenario_turn_counts.items():
        for sequence in range(1, turn_count + 1):
            failed = scenario_id == failed_scenario and sequence == turn_count
            results.append(
                QwenMultiTurnResult(
                    scenario_id=scenario_id,
                    turn_sequence=sequence,
                    context_passed=True,
                    model_behavior_passed=not failed,
                    memory_passed=not failed,
                    safety_passed=True,
                    passed=not failed,
                    failure_codes=["model_behavior_mismatch"] if failed else [],
                )
            )
    total = len(results)
    passed = sum(result.passed for result in results)
    return QwenMultiTurnTrialSummary(
        trial_number=trial_number,
        total_turns=total,
        passed_turns=passed,
        turn_pass_rate=passed / total,
        context_accuracy=1.0,
        model_behavior_accuracy=passed / total,
        memory_accuracy=passed / total,
        safety_accuracy=1.0,
        results=results,
    )


def test_multi_turn_candidate_budget_and_stable_promotion() -> None:
    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    config = load_qwen_multi_turn_config(
        "data/evaluation/qwen_multi_turn_experiment.json"
    )
    assert estimate_qwen_multi_turn_chat_calls(dataset, config) == 31
    assert enforce_qwen_multi_turn_budget(dataset, config) == 93
    counts = {
        scenario.scenario_id: len(scenario.turns)
        for scenario in dataset.scenarios
        if scenario.scenario_id in config.scenario_ids
    }
    trials = [_trial(number, counts) for number in range(1, 4)]

    report = summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model="offline-result-double",
        trials=trials,
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )

    assert report.mean_turn_pass_rate == 1.0
    assert report.worst_scenario_pass_rate == 1.0
    assert report.cross_trial_instability_rate == 0.0
    assert report.promotion_gate_passed is True
    payload = report.model_dump_json()
    assert "查询订单" not in payload
    assert "assistant_answer" not in payload


def test_intermittent_scenario_is_visible_and_fails_strict_gate() -> None:
    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    config = override_qwen_multi_turn_trials(
        load_qwen_multi_turn_config(
            "data/evaluation/qwen_multi_turn_experiment.json"
        ),
        2,
    )
    config = config.model_copy(
        update={
            "thresholds": config.thresholds.model_copy(
                update={
                    "min_mean_turn_pass_rate": 1.0,
                    "min_worst_scenario_pass_rate": 1.0,
                    "min_fully_stable_scenario_rate": 1.0,
                    "max_cross_trial_instability_rate": 0.0,
                }
            )
        }
    )
    counts = {
        scenario.scenario_id: len(scenario.turns)
        for scenario in dataset.scenarios
        if scenario.scenario_id in config.scenario_ids
    }
    trials = [
        _trial(1, counts),
        _trial(2, counts, failed_scenario="owned-order-follow-up"),
    ]

    report = summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model="unstable-result-double",
        trials=trials,
    )

    unstable = report.scenario_stability[0]
    assert unstable.pass_rate == 0.5
    assert unstable.observed_failure_codes == ["model_behavior_mismatch"]
    assert report.cross_trial_instability_rate == pytest.approx(0.2)
    assert report.promotion_gate_passed is False
    assert report.promotion_gate_failures == [
        "mean_turn_pass_rate_below_threshold",
        "worst_scenario_pass_rate_below_threshold",
        "fully_stable_scenario_rate_below_threshold",
        "cross_trial_instability_rate_above_threshold",
    ]


def test_budget_fails_before_target_creation() -> None:
    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    config = load_qwen_multi_turn_config(
        "data/evaluation/qwen_multi_turn_experiment.json"
    ).model_copy(update={"max_planned_chat_calls": 92})

    with pytest.raises(ValueError, match="超过预算"):
        enforce_qwen_multi_turn_budget(dataset, config)


@pytest.mark.asyncio
async def test_multi_turn_runner_shares_state_with_offline_target() -> None:
    """零费用完整运行器替身证明第二轮确实消费第一轮可信记忆。"""

    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    config = load_qwen_multi_turn_config(
        "data/evaluation/qwen_multi_turn_experiment.json"
    ).model_copy(
        update={
            "trials": 1,
            "max_planned_chat_calls": 40,
        }
    )

    report = await run_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model="offline-runner-double",
        target_factory=build_offline_agent_evaluation_target,
    )

    assert report.offline_control_gate_passed is True
    assert report.offline_control is not None
    assert report.offline_control.passed_turns == 11
    assert report.trials[0].passed_turns == 11
    assert report.trials[0].results[1].context_passed is True
    assert report.trials[0].results[1].memory_passed is True
    assert report.promotion_gate_passed is True


@pytest.mark.asyncio
async def test_paid_target_is_never_built_when_offline_control_fails() -> None:
    """金标与确定性整图冲突时必须在候选工厂和任何付费调用前失败。"""

    dataset = load_conversation_stability_dataset(
        "data/evaluation/conversation_stability_cases.json"
    )
    first_scenario = dataset.scenarios[0]
    bad_second_turn = first_scenario.turns[1].model_copy(
        update={"simulated_intent": Intent.HUMAN_HANDOFF}
    )
    bad_dataset = dataset.model_copy(
        update={
            "scenarios": [
                first_scenario.model_copy(
                    update={"turns": [first_scenario.turns[0], bad_second_turn]}
                ),
                *dataset.scenarios[1:],
            ]
        }
    )
    config = override_qwen_multi_turn_trials(
        load_qwen_multi_turn_config(
            "data/evaluation/qwen_multi_turn_experiment.json"
        ),
        1,
    )
    candidate_factory_called = False

    def forbidden_candidate_factory():
        nonlocal candidate_factory_called
        candidate_factory_called = True
        raise AssertionError("离线对照失败后不得创建真实候选目标")

    with pytest.raises(ValueError, match="离线对照未通过"):
        await run_qwen_multi_turn_experiment(
            dataset=bad_dataset,
            config=config,
            candidate_model="must-not-run",
            target_factory=forbidden_candidate_factory,
        )
    assert candidate_factory_called is False
