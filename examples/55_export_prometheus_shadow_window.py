"""第55步：从本地Prometheus导出影子窗口并计算发布建议。"""

import argparse

from serviceops_agent.application.conversation_shadow import (
    evaluate_shadow_release,
    load_shadow_alert_policy,
)
from serviceops_agent.application.prometheus_shadow import (
    PrometheusShadowClient,
    read_shadow_window,
)
from serviceops_agent.config.paths import resolve_project_path


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出Prometheus多轮影子窗口")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument(
        "--candidate-id",
        required=True,
        help="只导出该部署级候选身份的窗口",
    )
    parser.add_argument(
        "--policy",
        default="data/evaluation/conversation_shadow_alert_policy.json",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
    )
    parser.add_argument(
        "--decision",
        default=None,
    )
    parser.add_argument("--allow-non-continue", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    client = PrometheusShadowClient(
        str(arguments.prometheus_url),
        timeout_seconds=float(arguments.timeout_seconds),
    )
    candidate_id = str(arguments.candidate_id)
    snapshot = read_shadow_window(client, candidate_id)
    policy = load_shadow_alert_policy(str(arguments.policy))
    decision = evaluate_shadow_release(snapshot, policy)
    snapshot_path = resolve_project_path(
        str(arguments.snapshot)
        if arguments.snapshot is not None
        else f"data/runtime/conversation_shadow_{candidate_id}_window.json"
    )
    decision_path = resolve_project_path(
        str(arguments.decision)
        if arguments.decision is not None
        else f"data/runtime/conversation_shadow_{candidate_id}_release_decision.json"
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    decision_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    decision_path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
    print("=== Prometheus多轮影子窗口 ===")
    print(f"候选：{candidate_id}")
    print(f"样本：{snapshot.total_observations}")
    print(f"模型故障率：{snapshot.model_failure_rate:.2%}")
    print(f"证据拒答率：{snapshot.evidence_abstention_rate:.2%}")
    print(f"上下文歧义率：{snapshot.ambiguous_context_rate:.2%}")
    print(f"人工转接率：{snapshot.human_handoff_rate:.2%}")
    print(f"安全违规轮次：{snapshot.safety_violations}")
    print(f"建议：{decision.action.upper()}")
    print(f"窗口：{snapshot_path}")
    print(f"决策：{decision_path}")
    if decision.action == "continue" or bool(arguments.allow_non_continue):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
