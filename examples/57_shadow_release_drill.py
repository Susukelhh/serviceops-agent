"""第57步：离线演练影子窗口在各阈值下的发布动作。"""

from pydantic import BaseModel

from serviceops_agent.application.conversation_shadow import (
    ShadowReleaseDecision,
    ShadowWindowSnapshot,
    evaluate_shadow_release,
    load_shadow_alert_policy,
)
from serviceops_agent.config.paths import resolve_project_path


class DrillResult(BaseModel):
    drill_id: str
    expected_action: str
    passed: bool
    decision: ShadowReleaseDecision


class DrillReport(BaseModel):
    policy_id: str
    policy_version: str
    total_cases: int
    passed_cases: int
    results: list[DrillResult]


def _snapshot(
    *,
    total: int,
    model_failures: int = 0,
    evidence_abstentions: int = 0,
    ambiguous_contexts: int = 0,
    human_handoffs: int = 0,
    safety_violations: int = 0,
) -> ShadowWindowSnapshot:
    def rate(value: int) -> float:
        return value / total if total else 0.0

    return ShadowWindowSnapshot(
        candidate_id="drill-candidate",
        total_observations=total,
        model_failures=model_failures,
        evidence_abstentions=evidence_abstentions,
        ambiguous_contexts=ambiguous_contexts,
        human_handoffs=human_handoffs,
        safety_violations=safety_violations,
        model_failure_rate=rate(model_failures),
        evidence_abstention_rate=rate(evidence_abstentions),
        ambiguous_context_rate=rate(ambiguous_contexts),
        human_handoff_rate=rate(human_handoffs),
        safety_violation_rate=rate(safety_violations),
        safety_violation_code_counts=(
            {"injected_safety_violation": safety_violations}
            if safety_violations
            else {}
        ),
    )


def main() -> int:
    policy = load_shadow_alert_policy(
        "data/evaluation/conversation_shadow_alert_policy.json"
    )
    cases = [
        ("insufficient-sample", "observe", _snapshot(total=99)),
        ("immediate-safety-rollback", "rollback", _snapshot(total=1, safety_violations=1)),
        ("model-failure-rollback", "rollback", _snapshot(total=100, model_failures=6)),
        (
            "evidence-abstention-investigate",
            "investigate",
            _snapshot(total=100, evidence_abstentions=31),
        ),
        (
            "ambiguity-investigate",
            "investigate",
            _snapshot(total=100, ambiguous_contexts=36),
        ),
        (
            "handoff-investigate",
            "investigate",
            _snapshot(total=100, human_handoffs=41),
        ),
        (
            "exact-thresholds-continue",
            "continue",
            _snapshot(
                total=100,
                model_failures=5,
                evidence_abstentions=30,
                ambiguous_contexts=35,
                human_handoffs=40,
            ),
        ),
    ]
    results: list[DrillResult] = []
    for drill_id, expected_action, snapshot in cases:
        decision = evaluate_shadow_release(snapshot, policy)
        results.append(
            DrillResult(
                drill_id=drill_id,
                expected_action=expected_action,
                passed=decision.action == expected_action,
                decision=decision,
            )
        )
    report_path = resolve_project_path(
        "data/runtime/conversation_shadow_step57_release_drill.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    passed_cases = sum(result.passed for result in results)
    report = DrillReport(
        policy_id=policy.policy_id,
        policy_version=policy.version,
        total_cases=len(results),
        passed_cases=passed_cases,
        results=results,
    )
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print("=== 影子发布动作演练 ===")
    for result in results:
        print(
            f"{result.drill_id}: expected={result.expected_action} "
            f"actual={result.decision.action} passed={result.passed}"
        )
    print(f"结果：{passed_cases}/{len(results)}")
    print(f"报告：{report_path}")
    return 0 if passed_cases == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
