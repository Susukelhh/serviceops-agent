"""第51步：比较两份千问多轮证据并生成低敏JSON/HTML看板。"""

import argparse

from serviceops_agent.evaluation import (
    compare_qwen_candidate_evidence,
    load_qwen_candidate_comparison_policy,
    load_qwen_multi_turn_evidence_bundle,
    write_qwen_candidate_comparison_artifacts,
)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="比较两份千问多轮候选证据")
    parser.add_argument("--baseline-evidence", required=True)
    parser.add_argument("--candidate-evidence", required=True)
    parser.add_argument(
        "--policy",
        default="data/evaluation/qwen_candidate_comparison_policy.json",
    )
    parser.add_argument(
        "--output-directory",
        default="data/runtime/qwen_candidate_comparisons",
    )
    parser.add_argument("--allow-comparison-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    baseline = load_qwen_multi_turn_evidence_bundle(
        str(arguments.baseline_evidence)
    )
    candidate = load_qwen_multi_turn_evidence_bundle(
        str(arguments.candidate_evidence)
    )
    policy = load_qwen_candidate_comparison_policy(str(arguments.policy))
    report = compare_qwen_candidate_evidence(
        baseline=baseline,
        candidate=candidate,
        policy=policy,
    )
    json_path, html_path = write_qwen_candidate_comparison_artifacts(
        report=report,
        output_directory=str(arguments.output_directory),
    )
    print("=== 千问多轮候选版本对比 ===")
    print(f"可比：{'YES' if report.comparable else 'NO'}")
    print(f"平均轮次变化：{report.mean_turn_pass_rate_delta:+.2%}")
    print(f"回归场景：{report.regressed_scenarios}")
    print(f"待人工复核：{len(report.regression_queue)}")
    print(f"对比门：{'PASS' if report.comparison_gate_passed else 'FAIL'}")
    print(f"JSON：{json_path}")
    print(f"HTML：{html_path}")
    if report.comparison_gate_failures:
        print("失败：" + ", ".join(report.comparison_gate_failures))
    if report.comparison_gate_passed or bool(arguments.allow_comparison_failure):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
