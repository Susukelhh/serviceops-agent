"""第31步：离线比较四种检索路线，并在冻结后一次验收新 holdout。"""

# argparse 提供显式锁定集确认；json/Path 保存可审计报告。
import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from serviceops_agent.config.paths import PROJECT_ROOT
from serviceops_agent.evaluation.rag_hybrid_experiment import (
    RAGHybridProfileResult,
    load_rag_hybrid_experiment_config,
    run_rag_hybrid_experiment,
)

# 配置文件记录数据、候选参数、冻结名称和质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_hybrid_experiment.json"
# runtime 报告只保存本机最近一次结果，不提交到公开仓库。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_hybrid_experiment_report.json"


def _parse_args() -> argparse.Namespace:
    """默认只跑开发集，显式参数才允许读取新的锁定集。"""

    parser = argparse.ArgumentParser(description="运行零费用完整混合召回对照实验。")
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="确认开发优胜参数已冻结，并只运行一次新 holdout。",
    )
    return parser.parse_args()


def _print_results(title: str, results: Sequence[RAGHybridProfileResult]) -> None:
    """用固定列打印四路指标，便于初学者直接横向比较。"""

    print(f"\n{title}：")
    print("Profile                         Recall  Top1   MRR    nDCG   Gain   FPR    Gate")
    for result in results:
        # metrics 是统一检索指标的短别名，减少表格格式化表达式的重复。
        metrics = result.metrics
        # mode 保存当前路线类型，用于区分参考组和待晋级候选。
        mode = result.mode
        gate = (
            "REF"
            if mode != "hybrid_rrf"
            else ("PASS" if result.quality_gate_passed else "FAIL")
        )
        print(
            f"{result.profile_id:<31} "
            f"{metrics.recall_at_k:.3f}   {metrics.top_1_accuracy:.3f}  "
            f"{metrics.mrr_at_k:.3f}  {metrics.ndcg_at_k:.3f}  "
            f"{result.top_1_gain_vs_dense:+.3f}  "
            f"{metrics.false_positive_rate:.3f}  {gate}"
        )


def main() -> int:
    """运行实验、写报告，并用退出码提醒是否已经冻结或通过。"""

    args = _parse_args()
    config = load_rag_hybrid_experiment_config(CONFIG_PATH)
    report = run_rag_hybrid_experiment(config, include_holdout=args.confirm_holdout)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== ServiceOps 第31步：独立 Qdrant + 完整混合召回实验 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}；付费API：否")
    print(f"开发样本：{report.development_case_count} 条")
    _print_results("开发集四路对照与 RRF 参数扫描", report.development_results)
    print(f"\n开发集优胜候选：{report.selected_profile_id}")
    print(f"冻结候选匹配：{report.frozen_profile_matches_selection}")
    print(f"Dense+Lexical 理论并集 Recall：{report.dense_lexical_union_recall_at_k:.2%}")
    print(f"关键词救回的 Dense 漏召回题：{len(report.lexical_rescue_case_ids)} 条")
    if report.holdout_candidate is None:
        print("新混合召回锁定集：未运行")
        print(f"报告：{REPORT_PATH}")
        return 0 if report.frozen_profile_matches_selection else 1
    _print_results("冻结后一次锁定验收", report.holdout_results or [])
    print(
        f"锁定质量门：{'PASS' if report.holdout_candidate.quality_gate_passed else 'FAIL'}"
    )
    print(f"报告：{REPORT_PATH}")
    return 0 if report.holdout_candidate.quality_gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
