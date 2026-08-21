"""第26步：比较向量原序与BM25候选重排，不调用付费API。"""

# argparse提供显式holdout确认参数。
import argparse

# json写入UTF-8实验报告；Path声明固定项目路径。
import json
from pathlib import Path

# PROJECT_ROOT让PyCharm工作目录变化不影响文件定位。
from serviceops_agent.config.paths import PROJECT_ROOT

# 排序实验加载器和运行器封装候选选择与锁定保护。
from serviceops_agent.evaluation import (
    load_rag_rerank_experiment_config,
    run_rag_rerank_experiment,
)

# CONFIG_PATH保存权重列表、数据路径与预先声明质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_rerank_experiment.json"
# REPORT_PATH保存最近一次开发或锁定实验结果。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_v2_rerank_experiment_report.json"


def _parse_args() -> argparse.Namespace:
    """解析是否确认运行排序锁定集。"""

    # parser说明本脚本研究候选排序。
    parser = argparse.ArgumentParser(description="运行零费用RAG候选重排实验。")
    # 默认不读取holdout。
    parser.add_argument(
        "--confirm-holdout",
        # 参数出现时设为True。
        action="store_true",
        # 帮助文本强调候选必须先冻结。
        help="确认开发优胜权重已冻结，并运行一次排序holdout。",
    )
    # 返回结构化参数。
    return parser.parse_args()


def main() -> int:
    """运行排序候选并打印可比较指标。"""

    # 读取显式确认标记。
    args = _parse_args()
    # 加载版本化实验契约。
    config = load_rag_rerank_experiment_config(CONFIG_PATH)
    # 运行开发集，或在冻结匹配时附加holdout。
    report = run_rag_rerank_experiment(
        # 传入配置。
        config,
        # 只把显式参数映射到holdout开关。
        include_holdout=args.confirm_holdout,
    )
    # runtime目录可能尚不存在。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 写入强类型报告。
    REPORT_PATH.write_text(
        # JSON模式处理datetime，中文保持可读。
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        # 明确UTF-8。
        encoding="utf-8",
    )

    # 标题说明零费用与排序目标。
    print("=== ServiceOps RAG 第26步：候选重排实验 ===")
    # 打印版本与费用边界。
    print(f"实验：{report.experiment_id} v{report.experiment_version}；付费API：否")
    # 打印开发对比表头。
    print("\n开发集：")
    print("Profile                 Recall  Top1   MRR    nDCG   Gain   Gate")
    # 逐Profile输出排序指标。
    for result in report.development_results:
        # m是统一指标短别名。
        m = result.metrics
        # Baseline显示REF，候选显示真实质量门结果。
        gate_label = (
            # 原序只是参考组。
            "REF"
            # 候选根据联合门显示PASS/FAIL。
            if result.reranker == "off"
            else ("PASS" if result.quality_gate_passed else "FAIL")
        )
        # 固定三位小数方便纵向比较。
        print(
            f"{result.profile_id:<23} {m.recall_at_k:.3f}   "
            f"{m.top_1_accuracy:.3f}  {m.mrr_at_k:.3f}  "
            f"{m.ndcg_at_k:.3f}  {result.top_1_gain:+.3f}  {gate_label}"
        )
    # 打印只由开发集选出的优胜候选。
    print(f"\n开发集优胜候选：{report.selected_profile_id}")
    # 打印冻结匹配状态。
    print(f"冻结候选匹配：{report.frozen_profile_matches_selection}")

    # 默认开发运行不会有锁定结果。
    if report.holdout_candidate is None or report.holdout_baseline is None:
        # 明确锁定集没有运行。
        print("排序锁定集：未运行")
        # 输出报告位置。
        print(f"报告：{REPORT_PATH}")
        # 开发选择与冻结名称一致才返回零；pending阶段预期返回1提醒更新冻结配置。
        return 0 if report.frozen_profile_matches_selection else 1

    # hb和hc分别是锁定原序与冻结候选。
    hb = report.holdout_baseline
    hc = report.holdout_candidate
    # 打印锁定集前后对比。
    print("\n排序锁定集一次验收：")
    print(
        f"Baseline Top-1={hb.metrics.top_1_accuracy:.3f}，"
        f"MRR={hb.metrics.mrr_at_k:.3f}"
    )
    print(
        f"Candidate Top-1={hc.metrics.top_1_accuracy:.3f}，"
        f"MRR={hc.metrics.mrr_at_k:.3f}，"
        f"Gain={hc.top_1_gain:+.3f}"
    )
    # 输出最终质量门。
    print(f"锁定质量门：{'PASS' if hc.quality_gate_passed else 'FAIL'}")
    # 输出报告位置。
    print(f"报告：{REPORT_PATH}")
    # 只有冻结候选通过锁定门才正常退出。
    return 0 if hc.quality_gate_passed else 1


# 直接运行时把main退出码交给进程。
if __name__ == "__main__":
    # PyCharm会显示0或1。
    raise SystemExit(main())
