"""第51步：千问候选证据对比、失败分层与回归队列。"""

import html
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.evaluation.qwen_multi_turn_evidence import (
    QwenMultiTurnEvidenceBundle,
)


class CandidateFailureCategory(StrEnum):
    """有限失败层级，禁止把异常或模型正文当分类标签。"""

    CONTEXT = "context_resolution"
    MODEL = "model_behavior"
    MEMORY = "memory_projection"
    SAFETY = "safety_boundary"
    UNKNOWN = "unknown"


class QwenCandidateComparisonPolicy(BaseModel):
    """版本化的可比性、回归和安全门。"""

    policy_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=30)
    require_same_dataset_sha256: bool = True
    require_same_config_sha256: bool = True
    require_same_candidate_profile: bool = True
    require_candidate_promotion_passed: bool = True
    min_mean_turn_pass_rate_delta: float = Field(default=0.0, ge=-1.0, le=1.0)
    max_regressed_scenarios: int = Field(default=0, ge=0, le=100)
    allow_safety_accuracy_regression: bool = False


class QwenScenarioComparison(BaseModel):
    """同一场景在基线和候选证据中的变化。"""

    scenario_id: str
    baseline_pass_rate: float = Field(ge=0.0, le=1.0)
    candidate_pass_rate: float = Field(ge=0.0, le=1.0)
    pass_rate_delta: float = Field(ge=-1.0, le=1.0)
    outcome: Literal["improved", "regressed", "unchanged"]
    candidate_failure_codes: list[str]


class QwenRegressionQueueItem(BaseModel):
    """只引用已有金标位置的人工复核任务，不复制问题或回答。"""

    item_id: str = Field(pattern=r"^[A-Za-z0-9._:-]+$", max_length=240)
    scenario_id: str
    turn_sequence: int = Field(ge=1)
    failure_code: str
    category: CandidateFailureCategory
    severity: Literal["critical", "high", "medium"]
    occurrence_count: int = Field(ge=1)
    reproducibility: Literal["persistent", "intermittent"]
    review_status: Literal["needs_human_review"] = "needs_human_review"
    candidate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class QwenCandidateComparisonReport(BaseModel):
    """两个低敏证据包的可比性、指标变化、回归和发布结论。"""

    policy_id: str
    policy_version: str
    baseline_run_id: str
    candidate_run_id: str
    baseline_candidate_fingerprint: str
    candidate_candidate_fingerprint: str
    comparable: bool
    comparability_failures: list[str]
    mean_turn_pass_rate_delta: float = Field(ge=-1.0, le=1.0)
    worst_scenario_pass_rate_delta: float = Field(ge=-1.0, le=1.0)
    fully_stable_scenario_rate_delta: float = Field(ge=-1.0, le=1.0)
    cross_trial_instability_rate_delta: float = Field(ge=-1.0, le=1.0)
    safety_accuracy_delta: float = Field(ge=-1.0, le=1.0)
    improved_scenarios: int = Field(ge=0)
    regressed_scenarios: int = Field(ge=0)
    unchanged_scenarios: int = Field(ge=0)
    scenario_comparisons: list[QwenScenarioComparison]
    failure_category_counts: dict[CandidateFailureCategory, int]
    regression_queue: list[QwenRegressionQueueItem]
    comparison_gate_passed: bool
    comparison_gate_failures: list[str]


def load_qwen_candidate_comparison_policy(
    path: str,
) -> QwenCandidateComparisonPolicy:
    raw = resolve_project_path(path).read_text(encoding="utf-8")
    return TypeAdapter(QwenCandidateComparisonPolicy).validate_json(raw)


def load_qwen_multi_turn_evidence_bundle(
    path: str,
) -> QwenMultiTurnEvidenceBundle:
    raw = resolve_project_path(path).read_text(encoding="utf-8")
    return TypeAdapter(QwenMultiTurnEvidenceBundle).validate_json(raw)


def classify_candidate_failure(code: str) -> CandidateFailureCategory:
    """把第49步稳定失败码映射到明确负责层。"""

    mapping = {
        "context_resolution_mismatch": CandidateFailureCategory.CONTEXT,
        "model_behavior_mismatch": CandidateFailureCategory.MODEL,
        "memory_projection_mismatch": CandidateFailureCategory.MEMORY,
        "safety_invariant_failed": CandidateFailureCategory.SAFETY,
    }
    return mapping.get(code, CandidateFailureCategory.UNKNOWN)


def _build_regression_queue(
    candidate: QwenMultiTurnEvidenceBundle,
) -> list[QwenRegressionQueueItem]:
    occurrences: Counter[tuple[str, int, str]] = Counter()
    trial_count = candidate.report.trial_count
    for trial in candidate.report.trials:
        for result in trial.results:
            for code in result.failure_codes:
                occurrences[(result.scenario_id, result.turn_sequence, code)] += 1
    items: list[QwenRegressionQueueItem] = []
    for (scenario_id, turn_sequence, code), count in sorted(occurrences.items()):
        category = classify_candidate_failure(code)
        severity: Literal["critical", "high", "medium"]
        if category == CandidateFailureCategory.SAFETY:
            severity = "critical"
        elif category in {
            CandidateFailureCategory.CONTEXT,
            CandidateFailureCategory.MEMORY,
        }:
            severity = "high"
        else:
            severity = "medium"
        items.append(
            QwenRegressionQueueItem(
                item_id=f"{scenario_id}:{turn_sequence}:{code}",
                scenario_id=scenario_id,
                turn_sequence=turn_sequence,
                failure_code=code,
                category=category,
                severity=severity,
                occurrence_count=count,
                reproducibility=(
                    "persistent" if count == trial_count else "intermittent"
                ),
                candidate_fingerprint=(
                    candidate.manifest.candidate_fingerprint
                ),
            )
        )
    return items


def compare_qwen_candidate_evidence(
    *,
    baseline: QwenMultiTurnEvidenceBundle,
    candidate: QwenMultiTurnEvidenceBundle,
    policy: QwenCandidateComparisonPolicy,
) -> QwenCandidateComparisonReport:
    """比较两份已校验证据；不可比时仍产出诊断但绝不放行。"""

    comparability_failures: list[str] = []
    if (
        policy.require_same_dataset_sha256
        and baseline.manifest.dataset_sha256 != candidate.manifest.dataset_sha256
    ):
        comparability_failures.append("dataset_sha256_mismatch")
    if (
        policy.require_same_config_sha256
        and baseline.manifest.config_sha256 != candidate.manifest.config_sha256
    ):
        comparability_failures.append("config_sha256_mismatch")
    if (
        policy.require_same_candidate_profile
        and baseline.manifest.candidate_profile
        != candidate.manifest.candidate_profile
    ):
        comparability_failures.append("candidate_profile_mismatch")

    baseline_scenarios = {
        result.scenario_id: result
        for result in baseline.report.scenario_stability
    }
    candidate_scenarios = {
        result.scenario_id: result
        for result in candidate.report.scenario_stability
    }
    if set(baseline_scenarios) != set(candidate_scenarios):
        comparability_failures.append("scenario_set_mismatch")
    comparisons: list[QwenScenarioComparison] = []
    for scenario_id in sorted(set(baseline_scenarios) & set(candidate_scenarios)):
        old = baseline_scenarios[scenario_id]
        new = candidate_scenarios[scenario_id]
        delta = new.pass_rate - old.pass_rate
        outcome: Literal["improved", "regressed", "unchanged"]
        if delta > 0:
            outcome = "improved"
        elif delta < 0:
            outcome = "regressed"
        else:
            outcome = "unchanged"
        comparisons.append(
            QwenScenarioComparison(
                scenario_id=scenario_id,
                baseline_pass_rate=old.pass_rate,
                candidate_pass_rate=new.pass_rate,
                pass_rate_delta=delta,
                outcome=outcome,
                candidate_failure_codes=new.observed_failure_codes,
            )
        )

    mean_delta = (
        candidate.report.mean_turn_pass_rate
        - baseline.report.mean_turn_pass_rate
    )
    safety_delta = (
        candidate.report.mean_safety_accuracy
        - baseline.report.mean_safety_accuracy
    )
    regressed = sum(item.outcome == "regressed" for item in comparisons)
    gate_failures = list(comparability_failures)
    if (
        policy.require_candidate_promotion_passed
        and not candidate.report.promotion_gate_passed
    ):
        gate_failures.append("candidate_promotion_gate_failed")
    if mean_delta < policy.min_mean_turn_pass_rate_delta:
        gate_failures.append("mean_turn_pass_rate_delta_below_threshold")
    if regressed > policy.max_regressed_scenarios:
        gate_failures.append("regressed_scenarios_above_threshold")
    if not policy.allow_safety_accuracy_regression and safety_delta < 0:
        gate_failures.append("safety_accuracy_regressed")

    queue = _build_regression_queue(candidate)
    category_counts = Counter(item.category for item in queue)
    return QwenCandidateComparisonReport(
        policy_id=policy.policy_id,
        policy_version=policy.version,
        baseline_run_id=baseline.manifest.run_id,
        candidate_run_id=candidate.manifest.run_id,
        baseline_candidate_fingerprint=baseline.manifest.candidate_fingerprint,
        candidate_candidate_fingerprint=candidate.manifest.candidate_fingerprint,
        comparable=not comparability_failures,
        comparability_failures=comparability_failures,
        mean_turn_pass_rate_delta=mean_delta,
        worst_scenario_pass_rate_delta=(
            candidate.report.worst_scenario_pass_rate
            - baseline.report.worst_scenario_pass_rate
        ),
        fully_stable_scenario_rate_delta=(
            candidate.report.fully_stable_scenario_rate
            - baseline.report.fully_stable_scenario_rate
        ),
        cross_trial_instability_rate_delta=(
            candidate.report.cross_trial_instability_rate
            - baseline.report.cross_trial_instability_rate
        ),
        safety_accuracy_delta=safety_delta,
        improved_scenarios=sum(item.outcome == "improved" for item in comparisons),
        regressed_scenarios=regressed,
        unchanged_scenarios=sum(item.outcome == "unchanged" for item in comparisons),
        scenario_comparisons=comparisons,
        failure_category_counts=dict(sorted(category_counts.items())),
        regression_queue=queue,
        comparison_gate_passed=not gate_failures,
        comparison_gate_failures=gate_failures,
    )


def render_qwen_candidate_comparison_html(
    report: QwenCandidateComparisonReport,
) -> str:
    """生成无脚本静态看板；所有动态字段先HTML转义。"""

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.scenario_id)}</td>"
        f"<td>{item.baseline_pass_rate:.1%}</td>"
        f"<td>{item.candidate_pass_rate:.1%}</td>"
        f"<td>{item.pass_rate_delta:+.1%}</td>"
        f"<td>{html.escape(item.outcome)}</td>"
        "</tr>"
        for item in report.scenario_comparisons
    )
    failures = ", ".join(report.comparison_gate_failures) or "none"
    baseline_run = html.escape(report.baseline_run_id)
    candidate_run = html.escape(report.candidate_run_id)
    gate_class = "pass" if report.comparison_gate_passed else "fail"
    gate_label = "PASS" if report.comparison_gate_passed else "FAIL"
    mean_delta = f"{report.mean_turn_pass_rate_delta:+.1%}"
    safety_delta = f"{report.safety_accuracy_delta:+.1%}"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Qwen multi-turn candidate comparison</title>
<style>
body{{font-family:system-ui;margin:2rem;max-width:1100px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #bbb;padding:.5rem;text-align:left}}
.pass{{color:#087830}}.fail{{color:#b42318}}
</style>
</head><body><h1>千问多轮候选版本对比</h1>
<p>Baseline: {baseline_run}<br>Candidate: {candidate_run}</p>
<p class="{gate_class}">Comparison gate: {gate_label}</p>
<p>平均轮次变化：{mean_delta}；安全变化：{safety_delta}；失败门：{html.escape(failures)}</p>
<table><thead><tr><th>场景</th><th>基线</th><th>候选</th><th>变化</th><th>结论</th></tr></thead><tbody>{rows}</tbody></table>
<p>本看板不包含用户问题、模型答案、订单号或引用正文。</p></body></html>
"""


def write_qwen_candidate_comparison_artifacts(
    *,
    report: QwenCandidateComparisonReport,
    output_directory: str | Path,
) -> tuple[Path, Path]:
    """以候选指纹对命名JSON与HTML，不覆盖已有版本对比。"""

    output_root = resolve_project_path(output_directory)
    output_root.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{report.baseline_candidate_fingerprint[:12]}__to__"
        f"{report.candidate_candidate_fingerprint[:12]}"
    )
    json_path = output_root / f"{stem}.json"
    html_path = output_root / f"{stem}.html"
    if json_path.exists() or html_path.exists():
        raise FileExistsError("候选对比产物已存在，禁止覆盖")
    with json_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(report.model_dump_json(indent=2))
        handle.write("\n")
    try:
        with html_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(render_qwen_candidate_comparison_html(report))
    except Exception:
        json_path.unlink(missing_ok=True)
        raise
    return json_path, html_path
