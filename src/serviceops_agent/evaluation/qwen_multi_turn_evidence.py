"""第50步：真实千问多轮报告的校验、低敏诊断与不可覆盖证据包。"""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.evaluation.conversation_stability_evaluator import (
    ConversationStabilityDataset,
)
from serviceops_agent.evaluation.qwen_multi_turn_experiment import (
    QwenMultiTurnExperimentConfig,
    QwenMultiTurnExperimentReport,
    summarize_qwen_multi_turn_experiment,
)


class QwenMultiTurnFailureDiagnosis(BaseModel):
    """不含对话正文的失败分布和修复路由。"""

    failed_turns: int = Field(ge=0)
    failure_code_counts: dict[str, int]
    persistent_failure_scenario_ids: list[str]
    intermittent_failure_scenario_ids: list[str]
    recommended_investigation_codes: list[str]


class QwenMultiTurnEvidenceManifest(BaseModel):
    """把一次候选结论绑定到输入、代码、模型和原始报告摘要。"""

    schema_version: str = "qwen-multi-turn-evidence-v1"
    created_at: datetime
    run_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    source_revision: str = Field(pattern=r"^[A-Za-z0-9._/-]{1,160}$")
    experiment_id: str
    experiment_version: str
    dataset_id: str
    dataset_version: str
    candidate_model: str
    candidate_profile: str
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_recalculation_verified: bool
    budget_verified: bool
    promotion_gate_passed: bool
    promotion_gate_failures: list[str]
    diagnosis: QwenMultiTurnFailureDiagnosis


class QwenMultiTurnEvidenceBundle(BaseModel):
    """单文件保存清单与完整低敏报告，避免双文件提交中断。"""

    manifest: QwenMultiTurnEvidenceManifest
    report: QwenMultiTurnExperimentReport


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def diagnose_qwen_multi_turn_failures(
    report: QwenMultiTurnExperimentReport,
) -> QwenMultiTurnFailureDiagnosis:
    """聚合有限失败码，并区分持续失败和跨trial波动。"""

    all_results = [result for trial in report.trials for result in trial.results]
    counts = Counter(
        code for result in all_results for code in result.failure_codes
    )
    persistent = [
        scenario.scenario_id
        for scenario in report.scenario_stability
        if scenario.pass_rate == 0.0
    ]
    intermittent = [
        scenario.scenario_id
        for scenario in report.scenario_stability
        if 0.0 < scenario.pass_rate < 1.0
    ]
    routing: list[str] = []
    if counts.get("context_resolution_mismatch", 0):
        routing.append("inspect_context_resolution_and_memory_input")
    if counts.get("model_behavior_mismatch", 0):
        routing.append("inspect_qwen_classification_planning_or_grounding")
    if counts.get("memory_projection_mismatch", 0):
        routing.append("inspect_trusted_terminal_projection")
    if counts.get("safety_invariant_failed", 0):
        routing.append("block_promotion_and_inspect_safety_boundary")
    return QwenMultiTurnFailureDiagnosis(
        failed_turns=sum(not result.passed for result in all_results),
        failure_code_counts=dict(sorted(counts.items())),
        persistent_failure_scenario_ids=persistent,
        intermittent_failure_scenario_ids=intermittent,
        recommended_investigation_codes=routing,
    )


def _verify_report_recalculation(
    *,
    report: QwenMultiTurnExperimentReport,
    dataset: ConversationStabilityDataset,
    config: QwenMultiTurnExperimentConfig,
) -> None:
    """重新聚合逐轮结果，拒绝手改总指标或晋级布尔值。"""

    recalculated = summarize_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model=report.candidate_model,
        trials=report.trials,
        offline_control=report.offline_control,
        generated_at=report.generated_at,
    )
    if recalculated != report:
        raise ValueError("候选报告与逐轮结果重新计算值不一致")


def build_qwen_multi_turn_evidence_bundle(
    *,
    report_bytes: bytes,
    dataset_bytes: bytes,
    config_bytes: bytes,
    run_id: str,
    source_revision: str,
    created_at: datetime | None = None,
) -> QwenMultiTurnEvidenceBundle:
    """校验三份输入并构造可独立审计的不可变证据内容。"""

    report = QwenMultiTurnExperimentReport.model_validate_json(report_bytes)
    dataset = ConversationStabilityDataset.model_validate_json(dataset_bytes)
    config = QwenMultiTurnExperimentConfig.model_validate_json(config_bytes)
    _verify_report_recalculation(report=report, dataset=dataset, config=config)
    planned_total = report.planned_chat_calls_per_trial * report.trial_count
    budget_verified = (
        planned_total == report.planned_total_chat_calls
        and planned_total <= config.max_planned_chat_calls
        and report.budget_limit_chat_calls == config.max_planned_chat_calls
    )
    if not budget_verified:
        raise ValueError("候选报告预算字段与版本化配置不一致")
    report_sha = _sha256_bytes(report_bytes)
    dataset_sha = _sha256_bytes(dataset_bytes)
    config_sha = _sha256_bytes(config_bytes)
    fingerprint_payload = json.dumps(
        {
            "candidate_model": report.candidate_model,
            "candidate_profile": report.candidate_profile,
            "config_sha256": config_sha,
            "dataset_sha256": dataset_sha,
            "source_revision": source_revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest = QwenMultiTurnEvidenceManifest(
        created_at=created_at or datetime.now(UTC),
        run_id=run_id,
        source_revision=source_revision,
        experiment_id=report.experiment_id,
        experiment_version=report.experiment_version,
        dataset_id=report.dataset_id,
        dataset_version=report.dataset_version,
        candidate_model=report.candidate_model,
        candidate_profile=report.candidate_profile,
        report_sha256=report_sha,
        dataset_sha256=dataset_sha,
        config_sha256=config_sha,
        candidate_fingerprint=_sha256_bytes(fingerprint_payload),
        report_recalculation_verified=True,
        budget_verified=True,
        promotion_gate_passed=report.promotion_gate_passed,
        promotion_gate_failures=report.promotion_gate_failures,
        diagnosis=diagnose_qwen_multi_turn_failures(report),
    )
    return QwenMultiTurnEvidenceBundle(manifest=manifest, report=report)


def archive_qwen_multi_turn_evidence(
    *,
    bundle: QwenMultiTurnEvidenceBundle,
    output_directory: str | Path,
) -> Path:
    """以exclusive-create写入单文件；同一run_id绝不覆盖既有证据。"""

    output_root = resolve_project_path(output_directory)
    output_root.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{bundle.manifest.run_id}__"
        f"{bundle.manifest.candidate_fingerprint[:12]}.json"
    )
    output_path = output_root / filename
    with output_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(bundle.model_dump_json(indent=2))
        handle.write("\n")
    return output_path


def build_and_archive_qwen_multi_turn_evidence(
    *,
    report_path: str,
    dataset_path: str,
    config_path: str,
    output_directory: str,
    run_id: str,
    source_revision: str,
) -> Path:
    """CLI使用的文件边界：读取原始字节，校验后一次性归档。"""

    report_file = resolve_project_path(report_path)
    dataset_file = resolve_project_path(dataset_path)
    config_file = resolve_project_path(config_path)
    bundle = build_qwen_multi_turn_evidence_bundle(
        report_bytes=report_file.read_bytes(),
        dataset_bytes=dataset_file.read_bytes(),
        config_bytes=config_file.read_bytes(),
        run_id=run_id,
        source_revision=source_revision,
    )
    return archive_qwen_multi_turn_evidence(
        bundle=bundle,
        output_directory=output_directory,
    )
