"""第51步候选对比、失败分类、回归队列与静态看板测试。"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from serviceops_agent.evaluation import (
    CandidateFailureCategory,
    QwenMultiTurnResult,
    QwenMultiTurnTrialSummary,
    build_qwen_multi_turn_evidence_bundle,
    compare_qwen_candidate_evidence,
    load_conversation_stability_dataset,
    load_qwen_candidate_comparison_policy,
    load_qwen_multi_turn_config,
    render_qwen_candidate_comparison_html,
    summarize_qwen_multi_turn_experiment,
    write_qwen_candidate_comparison_artifacts,
)


def _bundle(
    *,
    run_id: str,
    source_revision: str,
    failure_code: str | None = None,
    failure_trial: int = 2,
):
    dataset_path = Path("data/evaluation/conversation_stability_cases.json")
    dataset = load_conversation_stability_dataset(str(dataset_path))
    config = load_qwen_multi_turn_config(
        "data/evaluation/qwen_multi_turn_experiment.json"
    ).model_copy(
        update={"trials": 2, "scenario_ids": ["owned-order-follow-up"]}
    )
    scenario = next(
        item
        for item in dataset.scenarios
        if item.scenario_id == "owned-order-follow-up"
    )
    trials: list[QwenMultiTurnTrialSummary] = []
    for trial_number in (1, 2):
        results: list[QwenMultiTurnResult] = []
        for sequence, _turn in enumerate(scenario.turns, start=1):
            failed = (
                failure_code is not None
                and trial_number == failure_trial
                and sequence == 2
            )
            context_passed = not (
                failed and failure_code == "context_resolution_mismatch"
            )
            model_passed = not (
                failed and failure_code == "model_behavior_mismatch"
            )
            memory_passed = not (
                failed and failure_code == "memory_projection_mismatch"
            )
            safety_passed = not (
                failed and failure_code == "safety_invariant_failed"
            )
            results.append(
                QwenMultiTurnResult(
                    scenario_id=scenario.scenario_id,
                    turn_sequence=sequence,
                    context_passed=context_passed,
                    model_behavior_passed=model_passed,
                    memory_passed=memory_passed,
                    safety_passed=safety_passed,
                    passed=not failed,
                    failure_codes=[failure_code] if failed and failure_code else [],
                )
            )
        passed = sum(item.passed for item in results)
        total = len(results)
        trials.append(
            QwenMultiTurnTrialSummary(
                trial_number=trial_number,
                total_turns=total,
                passed_turns=passed,
                turn_pass_rate=passed / total,
                context_accuracy=(
                    sum(item.context_passed for item in results) / total
                ),
                model_behavior_accuracy=(
                    sum(item.model_behavior_passed for item in results) / total
                ),
                memory_accuracy=sum(item.memory_passed for item in results) / total,
                safety_accuracy=sum(item.safety_passed for item in results) / total,
                results=results,
            )
        )
    report = summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model="qwen-test-candidate",
        trials=trials,
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    return build_qwen_multi_turn_evidence_bundle(
        report_bytes=report.model_dump_json(indent=2).encode("utf-8"),
        dataset_bytes=dataset_path.read_bytes(),
        config_bytes=config.model_dump_json(indent=2).encode("utf-8"),
        run_id=run_id,
        source_revision=source_revision,
        created_at=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )


def test_candidate_regression_creates_intermittent_review_queue() -> None:
    baseline = _bundle(run_id="baseline-001", source_revision="aaa111")
    candidate = _bundle(
        run_id="candidate-001",
        source_revision="bbb222",
        failure_code="model_behavior_mismatch",
    )
    policy = load_qwen_candidate_comparison_policy(
        "data/evaluation/qwen_candidate_comparison_policy.json"
    )

    report = compare_qwen_candidate_evidence(
        baseline=baseline,
        candidate=candidate,
        policy=policy,
    )

    assert report.comparable is True
    assert report.regressed_scenarios == 1
    assert report.mean_turn_pass_rate_delta == pytest.approx(-0.25)
    assert report.failure_category_counts == {
        CandidateFailureCategory.MODEL: 1
    }
    assert len(report.regression_queue) == 1
    queue_item = report.regression_queue[0]
    assert queue_item.reproducibility == "intermittent"
    assert queue_item.review_status == "needs_human_review"
    assert queue_item.category == CandidateFailureCategory.MODEL
    assert report.comparison_gate_passed is False
    assert report.comparison_gate_failures == [
        "candidate_promotion_gate_failed",
        "mean_turn_pass_rate_delta_below_threshold",
        "regressed_scenarios_above_threshold",
    ]


def test_safety_regression_is_critical_and_independently_blocked() -> None:
    baseline = _bundle(run_id="baseline-safe", source_revision="aaa111")
    candidate = _bundle(
        run_id="candidate-unsafe",
        source_revision="bbb222",
        failure_code="safety_invariant_failed",
    )
    policy = load_qwen_candidate_comparison_policy(
        "data/evaluation/qwen_candidate_comparison_policy.json"
    )

    report = compare_qwen_candidate_evidence(
        baseline=baseline,
        candidate=candidate,
        policy=policy,
    )

    assert report.safety_accuracy_delta == pytest.approx(-0.25)
    assert report.regression_queue[0].severity == "critical"
    assert "safety_accuracy_regressed" in report.comparison_gate_failures


def test_config_mismatch_is_not_presented_as_comparable() -> None:
    baseline = _bundle(run_id="baseline-config", source_revision="aaa111")
    candidate = _bundle(run_id="candidate-config", source_revision="bbb222")
    candidate.manifest.config_sha256 = "f" * 64
    policy = load_qwen_candidate_comparison_policy(
        "data/evaluation/qwen_candidate_comparison_policy.json"
    )

    report = compare_qwen_candidate_evidence(
        baseline=baseline,
        candidate=candidate,
        policy=policy,
    )

    assert report.comparable is False
    assert report.comparability_failures == ["config_sha256_mismatch"]
    assert report.comparison_gate_passed is False


def test_static_dashboard_and_json_are_low_sensitive_and_exclusive(
    tmp_path: Path,
) -> None:
    baseline = _bundle(run_id="baseline-html", source_revision="aaa111")
    candidate = _bundle(run_id="candidate-html", source_revision="bbb222")
    policy = load_qwen_candidate_comparison_policy(
        "data/evaluation/qwen_candidate_comparison_policy.json"
    )
    report = compare_qwen_candidate_evidence(
        baseline=baseline,
        candidate=candidate,
        policy=policy,
    )

    rendered = render_qwen_candidate_comparison_html(report)
    assert "Comparison gate: PASS" in rendered
    assert "查询订单" not in rendered
    assert "assistant_answer" not in rendered
    json_path, html_path = write_qwen_candidate_comparison_artifacts(
        report=report,
        output_directory=tmp_path,
    )
    assert json_path.exists() and html_path.exists()
    with pytest.raises(FileExistsError, match="禁止覆盖"):
        write_qwen_candidate_comparison_artifacts(
            report=report,
            output_directory=tmp_path,
        )
