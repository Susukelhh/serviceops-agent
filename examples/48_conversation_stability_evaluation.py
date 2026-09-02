"""第48步示例：运行零模型调用的多轮稳定性发布门。

运行：

    uv run python examples/48_conversation_stability_evaluation.py
"""

import argparse
import os

# 即使开发机.env配置了真实千问，本评测进程也只能使用离线状态控制层。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "false"

from serviceops_agent.config.paths import resolve_project_path
from serviceops_agent.evaluation import (
    evaluate_conversation_stability,
    load_conversation_stability_dataset,
)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行ServiceOps多轮稳定性离线发布门")
    parser.add_argument(
        "--dataset",
        default="data/evaluation/conversation_stability_cases.json",
    )
    parser.add_argument(
        "--report",
        default="data/runtime/conversation_stability_report.json",
    )
    parser.add_argument("--allow-gate-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    dataset = load_conversation_stability_dataset(str(arguments.dataset))
    summary = evaluate_conversation_stability(dataset)
    report_path = resolve_project_path(str(arguments.report))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    print("=== ServiceOps 多轮稳定性离线评测 ===")
    print(f"数据集：{summary.dataset_id} v{summary.dataset_version}")
    print(f"轮次通过：{summary.passed_turns}/{summary.total_turns}")
    print(f"指代解析：{summary.resolution_accuracy:.2%}")
    print(f"结构化记忆：{summary.memory_accuracy:.2%}")
    print(f"租约/重放安全：{summary.execution_safety_accuracy:.2%}")
    print(f"上下文隔离：{summary.isolation_accuracy:.2%}")
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    print(f"报告：{report_path}")
    for result in summary.results:
        if not result.passed:
            print(
                f"{result.scenario_id}#{result.turn_sequence}: "
                + ", ".join(result.failure_codes)
            )
    if summary.quality_gate_passed or bool(arguments.allow_gate_failure):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
