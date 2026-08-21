"""第27步：运行Hash与千问语义Embedding对照，默认不调用付费API。"""

# argparse提供付费和锁定集两道显式确认开关。
import argparse

# json用于保存UTF-8可审计报告。
import json

# Path声明固定配置与报告路径。
from pathlib import Path

# PROJECT_ROOT让PyCharm从任意工作目录启动都能定位项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings读取现有.env中的千问Key和兼容接口地址。
from serviceops_agent.config.settings import Settings

# 第27步实验加载器与运行器封装费用保护和阈值冻结。
from serviceops_agent.evaluation import (
    RAGSemanticProfileResult,
    load_rag_semantic_embedding_experiment_config,
    run_rag_semantic_embedding_experiment,
)

# CONFIG_PATH保存模型、数据、阈值候选、单价和质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_semantic_embedding_experiment.json"
# REPORT_PATH保存最近一次离线或真实实验报告。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_v2_semantic_embedding_experiment_report.json"


def _parse_args() -> argparse.Namespace:
    """解析真实费用和锁定集确认参数。"""

    # parser说明默认行为完全离线。
    parser = argparse.ArgumentParser(description="运行Hash与千问语义Embedding受控对照实验。")
    # 只有出现此参数才允许读取Key并调用真实Embedding。
    parser.add_argument(
        "--confirm-paid-api",
        # 参数出现时值为True。
        action="store_true",
        # 帮助文本明确会消费Embedding额度。
        help="确认调用真实千问Embedding并产生少量Token费用。",
    )
    # 锁定集需要第二个独立参数。
    parser.add_argument(
        "--confirm-holdout",
        # 参数出现时值为True。
        action="store_true",
        # 帮助文本强调先冻结开发阈值。
        help="确认开发优胜阈值已冻结，并运行一次语义锁定集。",
    )
    # 返回解析结果。
    return parser.parse_args()


def _print_table(title: str, results: list[RAGSemanticProfileResult]) -> None:
    """打印一组经过Pydantic校验的阈值结果。"""

    # 标题区分Hash、千问和锁定结果。
    print(f"\n{title}：")
    # 表头列出面试最常解释的五项指标。
    print("Profile                    Recall  Top1   MRR    Decision  FPR    Gate")
    # 逐个打印强类型结果。
    for result in results:
        # metrics保存统一指标。
        metrics = result.metrics
        # 质量门直接显示PASS/FAIL。
        gate_label = "PASS" if result.quality_gate_passed else "FAIL"
        # 固定三位小数方便比较阈值曲线。
        print(
            f"{result.profile_id:<26} "
            f"{metrics.recall_at_k:.3f}   {metrics.top_1_accuracy:.3f}  "
            f"{metrics.mrr_at_k:.3f}  {metrics.decision_accuracy:.3f}     "
            f"{metrics.false_positive_rate:.3f}  {gate_label}"
        )


def main() -> int:
    """运行实验、保存报告并给出下一步可执行提示。"""

    # 读取命令行双确认。
    args = _parse_args()
    # holdout必然会调用真实候选，因此缺第一把钥匙时立即拒绝。
    if args.confirm_holdout and not args.confirm_paid_api:
        # 抛出短错误，避免用户误以为锁定集可离线晋级真实模型。
        raise ValueError("--confirm-holdout必须同时提供--confirm-paid-api")
    # 加载版本化实验配置。
    config = load_rag_semantic_embedding_experiment_config(CONFIG_PATH)
    # Settings只提供.env中的密钥与地址；模型参数由实验配置冻结覆盖。
    settings = Settings()
    # 执行离线基线或显式确认的真实实验。
    report = run_rag_semantic_embedding_experiment(
        config,
        runtime_settings=settings,
        confirm_paid_api=args.confirm_paid_api,
        include_holdout=args.confirm_holdout,
    )
    # runtime目录可能在新克隆项目中尚不存在。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 报告写成中文可读UTF-8 JSON。
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 打印实验身份和固定规模。
    print("=== ServiceOps RAG 第27步：语义Embedding候选实验 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(
        f"语料：{report.document_count}份公开文档，{report.chunk_count}个切片；"
        f"候选模型：{report.model}"
    )
    # 明确费用计划和实际执行边界。
    print(
        f"首次开发实验计划请求：{report.planned_development_api_requests}次；"
        f"冻结后holdout增量：{report.planned_holdout_extra_api_requests}次"
    )
    # Hash阈值曲线始终可见。
    _print_table("Hash离线开发基线", list(report.hash_development_results))
    print(f"Hash开发选择阈值：{report.hash_selected_threshold:.2f}")

    # 没确认付费时只输出下一条明确命令语义，不自动继续。
    if not report.paid_api_called:
        print("\n真实千问Embedding：未调用（费用0元）")
        print("下一步确认后添加参数：--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线准备成功，返回0。
        return 0

    # 展示真实候选全部阈值，避免只挑最好数字。
    _print_table("千问真实语义开发候选", list(report.qwen_development_results))
    print(f"千问开发选择阈值：{report.qwen_selected_threshold:.2f}")
    print(f"冻结阈值匹配：{report.frozen_threshold_matches_selection}")
    # 服务商usage给出实际Token和成本，不用字符数冒充精确账单。
    print(
        f"实际成功API请求：{report.actual_api_requests}次；"
        f"输入Token：{report.actual_input_tokens}；"
        f"按公开原价估算：{report.actual_cost_cny:.6f}元"
    )

    # 未运行锁定集时根据冻结状态提示下一步。
    if report.qwen_holdout is None or report.hash_holdout is None:
        print("语义锁定集：未运行")
        print(f"报告：{REPORT_PATH}")
        # 开发候选通过且已经冻结才返回0；首次选择阶段返回1提醒需要冻结。
        return 0 if report.frozen_threshold_matches_selection else 1

    # 锁定阶段只展示一个预先冻结阈值。
    _print_table("Hash锁定对照", [report.hash_holdout])
    _print_table("千问锁定候选", [report.qwen_holdout])
    print(f"锁定质量门：{'PASS' if report.qwen_holdout.quality_gate_passed else 'FAIL'}")
    print(f"报告：{REPORT_PATH}")
    # 真实候选通过锁定门才返回0。
    return 0 if report.qwen_holdout.quality_gate_passed else 1


# 直接运行本文件时把main结果交给PyCharm进程窗口。
if __name__ == "__main__":
    # SystemExit使0/1退出码清晰可见。
    raise SystemExit(main())
