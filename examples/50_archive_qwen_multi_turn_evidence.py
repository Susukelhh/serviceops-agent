"""第50步：校验并不可覆盖地归档第49步真实候选报告。"""

import argparse
import os

from serviceops_agent.evaluation import build_and_archive_qwen_multi_turn_evidence


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="归档千问多轮候选晋级证据")
    parser.add_argument(
        "--report",
        default="data/runtime/qwen_multi_turn_experiment_report.json",
    )
    parser.add_argument(
        "--dataset",
        default="data/evaluation/conversation_stability_cases.json",
    )
    parser.add_argument(
        "--experiment-config",
        default="data/evaluation/qwen_multi_turn_experiment.json",
    )
    parser.add_argument(
        "--output-directory",
        default="data/runtime/qwen_multi_turn_evidence",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-revision", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _parse_arguments()
    output_path = build_and_archive_qwen_multi_turn_evidence(
        report_path=str(arguments.report),
        dataset_path=str(arguments.dataset),
        config_path=str(arguments.experiment_config),
        output_directory=str(arguments.output_directory),
        run_id=str(arguments.run_id),
        source_revision=str(arguments.source_revision),
    )
    print("=== 千问多轮候选证据已归档 ===")
    print(f"文件：{output_path}")
    print("报告指标已重新计算，输入哈希、源码revision和模型身份已绑定。")
    return 0


if __name__ == "__main__":
    # GitHub与本地都必须显式传入run ID/revision，不从报告正文猜测来源。
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
