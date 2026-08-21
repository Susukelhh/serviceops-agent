"""第25步：运行阈值扫描、FAQ范围门候选和可选锁定集验收。

开发集实验不会调用付费API：

    uv run python examples/25_rag_scope_candidate_experiment.py

候选冻结后，显式确认只运行一次锁定集：

    uv run python examples/25_rag_scope_candidate_experiment.py --confirm-holdout
"""

# argparse要求用户明确确认运行此前锁定的数据集。
import argparse

# json把强类型结果写成可审计UTF-8报告。
import json
from pathlib import Path

# PROJECT_ROOT保证PyCharm与终端路径一致。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第25步配置加载器和实验运行器封装全部候选逻辑。
from serviceops_agent.evaluation import (
    load_rag_scope_experiment_config,
    run_rag_scope_experiment,
)

# CONFIG_PATH是版本控制中的阈值、冻结候选和质量门契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_scope_experiment.json"
# REPORT_PATH保存最近一次开发或锁定实验结果。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_v2_scope_experiment_report.json"


def _parse_args() -> argparse.Namespace:
    """解析是否明确确认运行锁定集。"""

    # parser提供脚本用途和费用边界说明。
    parser = argparse.ArgumentParser(
        description="运行零费用RAG阈值与范围门实验。",
    )
    # 默认不运行holdout，避免普通点击Run时反复查看最终测试结果。
    parser.add_argument(
        "--confirm-holdout",
        # 该参数出现时写入True。
        action="store_true",
        # 帮助文本明确纪律而不是费用风险。
        help="确认候选已冻结，并运行一次锁定集泛化验收。",
    )
    # 返回结构化参数对象。
    return parser.parse_args()


def main() -> int:
    """执行候选实验、打印对比表并返回质量门退出码。"""

    # 读取命令行确认标记。
    args = _parse_args()
    # 加载版本化实验参数和预先声明的质量门。
    config = load_rag_scope_experiment_config(CONFIG_PATH)
    # 运行开发候选；只有显式参数为True才会读取holdout并执行search。
    report = run_rag_scope_experiment(
        # 传入强类型配置。
        config,
        # 把显式确认映射为锁定集开关。
        include_holdout=args.confirm_holdout,
    )
    # runtime目录在全新克隆中可能不存在。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 报告使用json模式转换datetime和Literal字段。
    REPORT_PATH.write_text(
        # 中文原因码和缩进方便现场查看。
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        # 明确UTF-8防止Windows默认编码乱码。
        encoding="utf-8",
    )

    # 标题明确本步只研究拒答边界。
    print("=== ServiceOps RAG 第25步：阈值与范围门实验 ===")
    # 输出实验版本和费用边界。
    print(f"实验：{report.experiment_id} v{report.experiment_version}；付费API：否")
    # 表头保持固定列宽，便于直接观察阈值取舍。
    print("\n开发集候选：")
    print("Profile                                  Recall  Top1   Decision  FPR    Gate")
    # 逐个输出阈值扫描和范围门候选。
    for result in report.development_results:
        # m是当前统一指标对象的短别名。
        m = result.metrics
        # 固定三位小数打印核心质量指标。
        print(
            f"{result.profile_id:<40} "
            f"{m.recall_at_k:.3f}   {m.top_1_accuracy:.3f}  "
            f"{m.decision_accuracy:.3f}     {m.false_positive_rate:.3f}  "
            f"{'PASS' if result.quality_gate_passed else 'FAIL'}"
        )
    # 输出只由开发集选出的Profile。
    print(f"\n开发集优胜候选：{report.selected_profile_id}")
    # 输出是否与运行holdout前写入配置的冻结候选一致。
    print(f"冻结候选匹配：{report.frozen_profile_matches_selection}")
    # 改善存在时打印绝对百分点，不使用模糊“提升很多”。
    if report.selected_false_positive_reduction is not None:
        # 转换为百分数百分点显示。
        print(
            "负例误召回率绝对下降："
            f"{report.selected_false_positive_reduction * 100:.1f} 个百分点"
        )
    # 同样打印决策准确率绝对提升。
    if report.selected_decision_accuracy_gain is not None:
        # 转换为百分数百分点显示。
        print(
            "决策准确率绝对提升："
            f"{report.selected_decision_accuracy_gain * 100:.1f} 个百分点"
        )

    # 用户未确认holdout时明确说明它仍未运行。
    if report.holdout_result is None:
        # 开发实验成功要求优胜候选与冻结配置一致。
        print("锁定集：未运行")
        # 打印报告绝对路径。
        print(f"报告：{REPORT_PATH}")
        # 匹配时开发阶段正常完成，否则返回非零阻止下一步。
        return 0 if report.frozen_profile_matches_selection else 1

    # h是锁定结果的短别名。
    h = report.holdout_result
    # 打印此前未参与选型的泛化指标。
    print("\n锁定集验收：")
    print(
        f"Recall@{h.metrics.top_k}={h.metrics.recall_at_k:.3f}，"
        f"Top-1={h.metrics.top_1_accuracy:.3f}，"
        f"Decision={h.metrics.decision_accuracy:.3f}，"
        f"FPR={h.metrics.false_positive_rate:.3f}"
    )
    # 显示锁定门最终结论。
    print(f"锁定集质量门：{'PASS' if h.quality_gate_passed else 'FAIL'}")
    # 输出报告位置。
    print(f"报告：{REPORT_PATH}")
    # 只有锁定质量门通过才返回零退出码。
    return 0 if h.quality_gate_passed else 1


# 直接运行脚本时把main返回值交给进程。
if __name__ == "__main__":
    # SystemExit让PyCharm清楚显示0或1。
    raise SystemExit(main())
