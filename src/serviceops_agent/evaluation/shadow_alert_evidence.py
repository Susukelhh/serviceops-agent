"""第58步：校验影子告警演练冻结证据与当前受控文件一致。"""

from hashlib import sha256
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter

from serviceops_agent.config.paths import resolve_project_path


class PromtoolEvidence(BaseModel):
    version: str
    rule_count: int = Field(ge=1)
    test_scenarios: int = Field(ge=1)
    passed_scenarios: int = Field(ge=0)
    result: str


class ApplicationDecisionEvidence(BaseModel):
    test_scenarios: int = Field(ge=1)
    passed_scenarios: int = Field(ge=0)
    result: str
    actions_verified: list[str]


class ShadowAlertDrillEvidence(BaseModel):
    drill_id: str
    scope: str
    paid_qwen_calls: int = Field(ge=0)
    promtool: PromtoolEvidence
    application_release_decisions: ApplicationDecisionEvidence
    external_deployment_rollback_executed: bool
    candidate_identity_isolated: bool = False
    sha256: dict[str, str]


class ShadowAlertEvidenceVerification(BaseModel):
    valid: bool
    mismatches: list[str]
    evidence: ShadowAlertDrillEvidence


EVIDENCE_HASH_PATHS = {
    "prometheus_rules": "deploy/observability/shadow-alert-rules.yaml",
    "promtool_tests": "deploy/observability/shadow-alert-tests.yaml",
    "shadow_policy": "data/evaluation/conversation_shadow_alert_policy.json",
    "application_drill_report": (
        "data/runtime/conversation_shadow_step57_release_drill.json"
    ),
    "runbook": "docs/runbooks/conversation-shadow-alert-response.md",
}


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def verify_shadow_alert_drill_evidence(
    evidence_path: str = (
        "data/evaluation/results/conversation_shadow_step59_final_result.json"
    ),
) -> ShadowAlertEvidenceVerification:
    """复算文件哈希并检查PASS证据的内部计数和付费边界。"""

    resolved_evidence_path = resolve_project_path(evidence_path)
    evidence = TypeAdapter(ShadowAlertDrillEvidence).validate_json(
        resolved_evidence_path.read_text(encoding="utf-8")
    )
    mismatches: list[str] = []
    if evidence.paid_qwen_calls != 0:
        mismatches.append("paid_qwen_calls_must_be_zero")
    if evidence.external_deployment_rollback_executed:
        mismatches.append("external_rollback_must_not_be_claimed")
    if not evidence.candidate_identity_isolated:
        mismatches.append("candidate_identity_not_isolated")
    if evidence.promtool.result != "PASS":
        mismatches.append("promtool_result_not_pass")
    if evidence.promtool.passed_scenarios != evidence.promtool.test_scenarios:
        mismatches.append("promtool_pass_count_mismatch")
    application = evidence.application_release_decisions
    if application.result != "PASS":
        mismatches.append("application_result_not_pass")
    if application.passed_scenarios != application.test_scenarios:
        mismatches.append("application_pass_count_mismatch")
    if set(application.actions_verified) != {
        "observe",
        "continue",
        "investigate",
        "rollback",
    }:
        mismatches.append("application_actions_incomplete")
    if set(evidence.sha256) != set(EVIDENCE_HASH_PATHS):
        mismatches.append("hash_manifest_keys_mismatch")
    for key, relative_path in EVIDENCE_HASH_PATHS.items():
        expected_hash = evidence.sha256.get(key)
        resolved_path = resolve_project_path(relative_path)
        if not resolved_path.is_file():
            mismatches.append(f"missing_file:{key}")
            continue
        if expected_hash != _file_sha256(resolved_path):
            mismatches.append(f"sha256_mismatch:{key}")
    return ShadowAlertEvidenceVerification(
        valid=not mismatches,
        mismatches=mismatches,
        evidence=evidence,
    )
