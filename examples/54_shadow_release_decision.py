"""第54步：根据低敏影子窗口快照计算继续、调查或回滚建议。"""

import argparse

from pydantic import TypeAdapter

from serviceops_agent.application.conversation_shadow import (
    ShadowWindowSnapshot,
    evaluate_shadow_release,
    load_shadow_alert_policy,
)
from serviceops_agent.config.paths import resolve_project_path


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="评估多轮影子窗口发布决策")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument(
        "--policy",
        default="data/evaluation/conversation_shadow_alert_policy.json",
    )
    parser.add_argument(
        "--report",
        default="data/runtime/conversation_shadow_release_decision.json",
    )
    parser.add_argument("--allow-non-continue", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    snapshot_path = resolve_project_path(str(arguments.snapshot))
    snapshot = TypeAdapter(ShadowWindowSnapshot).validate_json(
        snapshot_path.read_text(encoding="utf-8")
    )
    policy = load_shadow_alert_policy(str(arguments.policy))
    decision = evaluate_shadow_release(snapshot, policy)
    report_path = resolve_project_path(str(arguments.report))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
    print("=== 多轮影子窗口发布决策 ===")
    print(f"样本：{snapshot.total_observations}")
    print(f"安全违规：{snapshot.safety_violations}")
    print(f"模型故障率：{snapshot.model_failure_rate:.2%}")
    print(f"人工转接率：{snapshot.human_handoff_rate:.2%}")
    print(f"建议：{decision.action.upper()}")
    print(f"原因：{', '.join(decision.reason_codes) or 'none'}")
    print(f"报告：{report_path}")
    if decision.action == "continue" or bool(arguments.allow_non_continue):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
