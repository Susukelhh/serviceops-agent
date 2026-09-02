"""第50步候选证据重算、指纹、诊断与不可覆盖归档测试。"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from serviceops_agent.evaluation import (
    QwenMultiTurnResult,
    QwenMultiTurnTrialSummary,
    archive_qwen_multi_turn_evidence,
    build_qwen_multi_turn_evidence_bundle,
    load_conversation_stability_dataset,
    load_qwen_multi_turn_config,
    override_qwen_multi_turn_trials,
    summarize_qwen_multi_turn_experiment,
)


def _evidence_inputs(
    *,
    failed_second_trial: bool = False,
) -> tuple[bytes, bytes, bytes]:
    dataset_path = Path("data/evaluation/conversation_stability_cases.json")
    config_path = Path("data/evaluation/qwen_multi_turn_experiment.json")
    dataset_bytes = dataset_path.read_bytes()
    config_bytes = config_path.read_bytes()
    dataset = load_conversation_stability_dataset(str(dataset_path))
    config = override_qwen_multi_turn_trials(
        load_qwen_multi_turn_config(str(config_path)),
        2,
    )
    # 证据必须与实际配置字节一致，因此测试构造一个对应两轮的临时规范配置字节。
    config_bytes = config.model_dump_json(indent=2).encode("utf-8")
    trials: list[QwenMultiTurnTrialSummary] = []
    for trial_number in (1, 2):
        results: list[QwenMultiTurnResult] = []
        for scenario in dataset.scenarios:
            if scenario.scenario_id not in config.scenario_ids:
                continue
            for sequence, _turn in enumerate(scenario.turns, start=1):
                failed = (
                    failed_second_trial
                    and trial_number == 2
                    and scenario.scenario_id == "owned-order-follow-up"
                    and sequence == 2
                )
                results.append(
                    QwenMultiTurnResult(
                        scenario_id=scenario.scenario_id,
                        turn_sequence=sequence,
                        context_passed=True,
                        model_behavior_passed=not failed,
                        memory_passed=not failed,
                        safety_passed=True,
                        passed=not failed,
                        failure_codes=(
                            ["model_behavior_mismatch"] if failed else []
                        ),
                    )
                )
        total = len(results)
        passed = sum(result.passed for result in results)
        trials.append(
            QwenMultiTurnTrialSummary(
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
        )
    report = summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model="qwen-test-candidate",
        trials=trials,
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
    )
    return report.model_dump_json(indent=2).encode("utf-8"), dataset_bytes, config_bytes


def test_evidence_recalculates_and_binds_all_inputs(tmp_path: Path) -> None:
    report_bytes, dataset_bytes, config_bytes = _evidence_inputs()
    bundle = build_qwen_multi_turn_evidence_bundle(
        report_bytes=report_bytes,
        dataset_bytes=dataset_bytes,
        config_bytes=config_bytes,
        run_id="local-001",
        source_revision="codex/test-revision",
        created_at=datetime(2026, 8, 30, 1, tzinfo=UTC),
    )

    assert bundle.manifest.report_recalculation_verified is True
    assert bundle.manifest.budget_verified is True
    assert bundle.manifest.report_sha256 == hashlib.sha256(report_bytes).hexdigest()
    assert bundle.manifest.diagnosis.failed_turns == 0
    assert bundle.manifest.promotion_gate_passed is True
    output = archive_qwen_multi_turn_evidence(
        bundle=bundle,
        output_directory=tmp_path,
    )
    assert output.exists()
    persisted = output.read_text(encoding="utf-8")
    assert "查询订单" not in persisted
    assert "assistant_answer" not in persisted
    # exclusive-create保证同一run和候选指纹不能被后一次结果覆盖。
    with pytest.raises(FileExistsError):
        archive_qwen_multi_turn_evidence(
            bundle=bundle,
            output_directory=tmp_path,
        )


def test_evidence_rejects_manually_changed_aggregate() -> None:
    report_bytes, dataset_bytes, config_bytes = _evidence_inputs()
    payload = json.loads(report_bytes)
    payload["mean_turn_pass_rate"] = 0.123

    with pytest.raises(ValueError, match="重新计算值不一致"):
        build_qwen_multi_turn_evidence_bundle(
            report_bytes=json.dumps(payload).encode("utf-8"),
            dataset_bytes=dataset_bytes,
            config_bytes=config_bytes,
            run_id="tampered-001",
            source_revision="abc123",
        )


def test_evidence_diagnoses_intermittent_model_failure_without_text() -> None:
    report_bytes, dataset_bytes, config_bytes = _evidence_inputs(
        failed_second_trial=True
    )
    bundle = build_qwen_multi_turn_evidence_bundle(
        report_bytes=report_bytes,
        dataset_bytes=dataset_bytes,
        config_bytes=config_bytes,
        run_id="failure-001",
        source_revision="abc123",
    )

    diagnosis = bundle.manifest.diagnosis
    assert diagnosis.failed_turns == 1
    assert diagnosis.failure_code_counts == {"model_behavior_mismatch": 1}
    assert diagnosis.intermittent_failure_scenario_ids == [
        "owned-order-follow-up"
    ]
    assert diagnosis.recommended_investigation_codes == [
        "inspect_qwen_classification_planning_or_grounding"
    ]
