"""第29步：运行完整RAG组合链路实验，默认不调用任何付费API。"""

# argparse提供真实候选和锁定集两道显式确认开关。
import argparse

# asyncio运行异步Grounded回答器。
import asyncio

# json把强类型报告保存为可审计UTF-8文件。
import json

# Path声明稳定配置和报告路径类型。
from pathlib import Path

# PROJECT_ROOT保证PyCharm工作目录变化时仍定位项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings读取.env中的千问Key、地址、模型、超时和重试参数。
from serviceops_agent.config.settings import Settings

# 第29步公共加载器、运行器和汇总类型用于执行与展示。
from serviceops_agent.evaluation import (
    RAGEndToEndSummary,
    load_rag_end_to_end_experiment_config,
    run_rag_end_to_end_experiment,
)

# CONFIG_PATH保存数据路径、组合参数、冻结指纹和质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_end_to_end_experiment.json"
# REPORT_PATH保存最近一次离线、开发或锁定实验的完整逐题轨迹。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_end_to_end_experiment_report.json"


def _parse_args() -> argparse.Namespace:
    """解析真实千问和锁定集确认参数。"""

    # parser明确说明默认路径完全离线。
    parser = argparse.ArgumentParser(description="运行第29步端到端RAG组合验收。")
    # 第一把钥匙允许真实Embedding和真实Grounded聊天调用。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认调用千问Embedding和qwen-plus结构化回答。",
    )
    # 第二把钥匙只在完整候选开发通过并冻结后读取holdout。
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="确认候选指纹已冻结并执行一次全新端到端holdout。",
    )
    # 返回命令行解析结果。
    return parser.parse_args()


def _print_summary(title: str, summary: RAGEndToEndSummary) -> None:
    """打印检索、最终决策和安全指标，以及可定位的Bad Case。"""

    # 标题区分基线/候选与开发/锁定数据集。
    print(f"\n{title}：")
    # 第一行展示检索层指标。
    print(
        f"检索Recall={summary.retrieval_recall:.2%}，"
        f"Top-1={summary.top_1_accuracy:.2%}"
    )
    # 第二行展示完整链路最终业务指标。
    print(
        f"有答案召回={summary.answerable_recall:.2%}，"
        f"知识缺口正确拒答={summary.abstention_accuracy:.2%}，"
        f"综合决策={summary.decision_accuracy:.2%}，"
        f"无依据回答率={summary.unsupported_answer_rate:.2%}，"
        f"引用合法率={summary.citation_validity:.2%}，"
        f"Gate={'PASS' if summary.quality_gate_passed else 'FAIL'}"
    )
    # 打印实际进入回答器的题数，范围拒绝和空检索不会产生聊天调用。
    print(f"实际回答器调用：{summary.grounding_chat_calls}次")
    # 只选择未通过的逐题结果。
    failed_results = [result for result in summary.results if not result.passed]
    # 没有Bad Case时明确显示0条。
    if not failed_results:
        # 用户无需猜测是否输出被截断。
        print("失败样本：0条")
        # 提前结束，无需进入循环。
        return
    # 打印失败数量。
    print(f"失败样本：{len(failed_results)}条")
    # 逐条展示结束层、候选文档和有限原因码。
    for result in failed_results:
        # join形成便于复制搜索的稳定字符串。
        failures = ",".join(result.failure_codes)
        # 不打印模型答案正文或隐藏推理。
        print(
            f"- {result.case_id}：stage={result.terminal_stage}，"
            f"retrieved={result.retrieved_document_ids}，{failures}"
        )


async def _async_main() -> int:
    """执行实验、保存报告并根据质量门返回退出码。"""

    # 读取两道确认开关。
    args = _parse_args()
    # holdout必然需要真实候选，禁止只提供第二把钥匙。
    if args.confirm_holdout and not args.confirm_paid_api:
        # 在加载锁定数据前快速拒绝。
        raise ValueError("--confirm-holdout必须同时提供--confirm-paid-api")
    # 加载版本化实验契约。
    config = load_rag_end_to_end_experiment_config(CONFIG_PATH)
    # Settings读取项目.env；默认离线路径不会使用其中密钥。
    settings = Settings()
    # 运行离线基线或显式确认的真实组合候选。
    report = await run_rag_end_to_end_experiment(
        config,
        runtime_settings=settings,
        confirm_paid_api=args.confirm_paid_api,
        include_holdout=args.confirm_holdout,
    )
    # 新环境可能没有runtime目录，保存前显式创建。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic报告使用JSON模式处理日期等类型，中文不转义。
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 打印实验身份与完整候选指纹。
    print("=== ServiceOps 第29步：端到端RAG组合验收 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"候选：{report.candidate_profile_id}")
    print(f"Embedding：{report.embedding_model}；聊天模型：{report.chat_model}")
    print(f"候选指纹：{report.candidate_fingerprint}")
    # 费用计划分开显示开发和锁定增量。
    print(
        f"开发计划：Embedding约{report.planned_development_embedding_requests}次，"
        f"聊天最多{report.planned_development_chat_calls}次"
    )
    print(
        f"holdout增量：Embedding约{report.planned_holdout_extra_embedding_requests}次，"
        f"聊天最多{report.planned_holdout_extra_chat_calls}次"
    )
    # 离线基线始终存在。
    _print_summary("当前Hash+BM25+Extractive开发基线", report.baseline_development)

    # 默认模式到此结束，不产生费用。
    if not report.paid_api_called or report.candidate_development is None:
        # 明确真实服务没有被调用。
        print("\n真实端到端候选：未调用（费用0元）")
        print("确认后添加参数：--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线基线即使Gate失败也是预期诊断，进程正常退出。
        return 0

    # 展示真实组合开发候选。
    _print_summary("千问端到端开发候选", report.candidate_development)
    print(f"候选冻结匹配：{report.frozen_candidate_matches}")
    print(
        f"实际Embedding请求：{report.actual_embedding_requests}次；"
        f"输入Token：{report.actual_embedding_input_tokens}；"
        f"实际聊天调用：{report.actual_chat_calls}次"
    )

    # 没有运行锁定集时根据开发门与冻结状态给出退出码。
    if report.candidate_holdout is None or report.baseline_holdout is None:
        # 明确holdout仍未读取。
        print("端到端锁定集：未运行")
        print(f"报告：{REPORT_PATH}")
        # 第一次开发通常因指纹尚未冻结返回1。
        return 0 if report.frozen_candidate_matches else 1

    # 同时展示锁定集当前基线和冻结候选。
    _print_summary("当前链路锁定对照", report.baseline_holdout)
    _print_summary("千问端到端锁定候选", report.candidate_holdout)
    print(
        "锁定质量门："
        f"{'PASS' if report.candidate_holdout.quality_gate_passed else 'FAIL'}"
    )
    print(f"报告：{REPORT_PATH}")
    # 只有完整候选锁定门通过才返回0。
    return 0 if report.candidate_holdout.quality_gate_passed else 1


def main() -> int:
    """为PyCharm与普通Python入口创建并关闭异步事件循环。"""

    # asyncio.run负责事件循环生命周期。
    return asyncio.run(_async_main())


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit把质量门结论显示为进程退出码。
    raise SystemExit(main())
