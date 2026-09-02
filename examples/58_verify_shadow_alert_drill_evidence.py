"""第58步：CI复算影子告警演练冻结证据。"""

import argparse

from serviceops_agent.evaluation.shadow_alert_evidence import (
    verify_shadow_alert_drill_evidence,
)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验影子告警演练冻结证据")
    parser.add_argument(
        "--evidence",
        default="data/evaluation/results/conversation_shadow_step59_final_result.json",
    )
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    verification = verify_shadow_alert_drill_evidence(str(arguments.evidence))
    print("=== 影子告警演练证据校验 ===")
    print(f"演练：{verification.evidence.drill_id}")
    print(
        "Promtool："
        f"{verification.evidence.promtool.passed_scenarios}/"
        f"{verification.evidence.promtool.test_scenarios}"
    )
    application = verification.evidence.application_release_decisions
    print(f"应用决策：{application.passed_scenarios}/{application.test_scenarios}")
    print(f"证据：{'PASS' if verification.valid else 'FAIL'}")
    if verification.mismatches:
        print(f"不一致：{', '.join(verification.mismatches)}")
    return 0 if verification.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
