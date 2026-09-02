"""第58步影子告警冻结证据哈希与失败诊断测试。"""

import json
from pathlib import Path

from serviceops_agent.evaluation.shadow_alert_evidence import (
    verify_shadow_alert_drill_evidence,
)


def test_frozen_shadow_alert_evidence_matches_current_contract() -> None:
    verification = verify_shadow_alert_drill_evidence()

    assert verification.valid is True
    assert verification.mismatches == []
    assert verification.evidence.paid_qwen_calls == 0
    assert verification.evidence.promtool.passed_scenarios == 9
    assert verification.evidence.candidate_identity_isolated is True


def test_shadow_alert_evidence_reports_hash_mismatch(tmp_path: Path) -> None:
    source = Path(
        "data/evaluation/results/conversation_shadow_step59_final_result.json"
    )
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["sha256"]["prometheus_rules"] = "0" * 64
    tampered_path = tmp_path / "tampered-shadow-alert-evidence.json"
    tampered_path.write_text(json.dumps(payload), encoding="utf-8")

    verification = verify_shadow_alert_drill_evidence(str(tampered_path))

    assert verification.valid is False
    assert verification.mismatches == ["sha256_mismatch:prometheus_rules"]
