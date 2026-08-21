"""第28步：评测检索证据是否真正足以回答，默认不调用付费API。"""

# argparse提供真实聊天和锁定集两道确认开关。
import argparse

# asyncio运行异步Grounded回答客户端。
import asyncio

# json保存UTF-8实验报告。
import json

# Path声明固定项目路径。
from pathlib import Path

# PROJECT_ROOT避免PyCharm工作目录变化导致文件找不到。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings读取.env中的千问聊天模型、Key和Base URL。
from serviceops_agent.config.settings import Settings

# 第28步公共加载器、运行器和强类型结果用于展示。
from serviceops_agent.evaluation import (
    GroundingEvaluationSummary,
    load_grounding_sufficiency_experiment_config,
    run_grounding_sufficiency_experiment,
)

# CONFIG_PATH保存固定证据数据、提示冻结值和质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/grounding_sufficiency_experiment.json"
# REPORT_PATH保存最近一次离线、开发或锁定实验结果。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/grounding_sufficiency_report.json"


def _parse_args() -> argparse.Namespace:
    """解析真实千问与锁定集确认参数。"""

    # parser解释默认命令完全离线。
    parser = argparse.ArgumentParser(description="运行证据充分性与无依据回答控制实验。")
    # 第一把钥匙允许每题调用一次真实千问聊天模型。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认使用真实千问执行结构化is_answerable判断。",
    )
    # 第二把钥匙允许在提示冻结后读取新holdout。
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="确认提示已冻结并运行一次Grounding holdout。",
    )
    # 返回解析结果。
    return parser.parse_args()


def _print_summary(title: str, summary: GroundingEvaluationSummary) -> None:
    """用一行通俗指标展示回答器表现。"""

    # 标题说明回答器和数据集。
    print(f"\n{title}：")
    # 五项指标分别覆盖可用性、安全性和引用约束。
    print(
        f"有答案召回={summary.answerable_recall:.2%}，"
        f"知识缺口正确拒答={summary.abstention_accuracy:.2%}，"
        f"综合决策={summary.decision_accuracy:.2%}，"
        f"无依据回答率={summary.unsupported_answer_rate:.2%}，"
        f"引用合法率={summary.citation_validity:.2%}，"
        f"Gate={'PASS' if summary.quality_gate_passed else 'FAIL'}"
    )
    # 只打印失败样本ID和有限原因，不输出模型答案正文。
    failed_results = [result for result in summary.results if not result.passed]
    # 没有失败时明确打印0条。
    if not failed_results:
        # 便于用户快速确认。
        print("失败样本：0条")
        # 无需进入循环。
        return
    # 打印失败数量。
    print(f"失败样本：{len(failed_results)}条")
    # 逐条展示稳定ID、预测和原因。
    for result in failed_results:
        # joined_failures保持报告原因码顺序。
        joined_failures = ",".join(result.failure_codes)
        # False表示模型拒答，True表示模型选择回答。
        print(
            f"- {result.case_id}：predicted_answerable="
            f"{result.predicted_answerable}，{joined_failures}"
        )


async def _async_main() -> int:
    """执行实验、写报告并返回清晰退出码。"""

    # 解析双确认参数。
    args = _parse_args()
    # holdout必然需要真实候选，缺第一把钥匙时快速拒绝。
    if args.confirm_holdout and not args.confirm_paid_api:
        # 不读取任何数据或创建模型客户端。
        raise ValueError("--confirm-holdout必须同时提供--confirm-paid-api")
    # 加载版本化实验契约。
    config = load_grounding_sufficiency_experiment_config(CONFIG_PATH)
    # 读取项目现有千问配置；默认离线路径不会使用Key。
    settings = Settings()
    # 运行离线Baseline或显式确认的真实候选。
    report = await run_grounding_sufficiency_experiment(
        config,
        runtime_settings=settings,
        confirm_paid_api=args.confirm_paid_api,
        include_holdout=args.confirm_holdout,
    )
    # runtime目录在新环境中可能尚不存在。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 保存Pydantic报告，中文不转义。
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 打印实验身份和费用边界。
    print("=== ServiceOps 第28步：证据充分性与幻觉控制实验 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"候选：{report.candidate_profile_id}；模型：{report.candidate_model}")
    print(f"提示指纹：{report.prompt_sha256}")
    print(
        f"开发计划聊天调用：{report.planned_development_chat_calls}次；"
        f"holdout增量：{report.planned_holdout_extra_chat_calls}次"
    )
    # Extractive基线始终存在。
    _print_summary("Extractive开发基线", report.extractive_development)

    # 默认离线运行在此正常结束。
    if not report.paid_api_called or report.qwen_development is None:
        # 明确没有花费额度。
        print("\n真实千问Grounded候选：未调用（费用0元）")
        print("确认后添加参数：--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线基线成功暴露问题，退出0。
        return 0

    # 打印真实开发候选。
    _print_summary("千问Grounded开发候选", report.qwen_development)
    print(f"提示冻结匹配：{report.frozen_prompt_matches}")
    print(f"实际成功聊天调用：{report.actual_chat_calls}次")

    # 未运行holdout时根据开发门与冻结状态返回。
    if report.qwen_holdout is None or report.extractive_holdout is None:
        # 明确锁定集未运行。
        print("Grounding锁定集：未运行")
        print(f"报告：{REPORT_PATH}")
        # 首次开发通常因提示未冻结返回1，提醒审查结果。
        return 0 if report.frozen_prompt_matches else 1

    # 展示同一holdout前后对照。
    _print_summary("Extractive锁定对照", report.extractive_holdout)
    _print_summary("千问Grounded锁定候选", report.qwen_holdout)
    print(f"锁定质量门：{'PASS' if report.qwen_holdout.quality_gate_passed else 'FAIL'}")
    print(f"报告：{REPORT_PATH}")
    # 只有真实锁定候选通过才返回0。
    return 0 if report.qwen_holdout.quality_gate_passed else 1


def main() -> int:
    """为普通Python入口运行异步实验。"""

    # asyncio.run创建并关闭本次事件循环。
    return asyncio.run(_async_main())


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit把质量门状态显示为进程退出码。
    raise SystemExit(main())
