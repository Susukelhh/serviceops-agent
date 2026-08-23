"""第32步：先跑规则基线，再显式确认真实千问意图分类候选。"""

# argparse 保护付费调用和锁定集；asyncio 驱动异步 LangChain 分类客户端。
import argparse
import asyncio
import json
from pathlib import Path

from serviceops_agent.config.paths import PROJECT_ROOT
from serviceops_agent.config.settings import Settings
from serviceops_agent.evaluation.intent_classification_experiment import (
    IntentProfileResult,
    load_intent_experiment_config,
    run_intent_classification_experiment,
)
from serviceops_agent.llm.intent_classifier import LangChainIntentClassificationClient
from serviceops_agent.llm.provider import create_chat_model

# CONFIG_PATH 保存数据、模型、阈值候选、提示指纹和质量门。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/intent_classification_experiment.json"
# REPORT_PATH 是不进入 Git 的本机完整运行报告。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/intent_classification_report.json"


def _parse_args() -> argparse.Namespace:
    """真实模型和holdout都必须由用户显式确认。"""

    parser = argparse.ArgumentParser(description="运行意图分类与语言漂移专项实验。")
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认使用当前.env中的千问Key运行开发集真实聊天调用。",
    )
    parser.add_argument(
        "--confirm-holdout",
        action="store_true",
        help="确认候选阈值已冻结，并一次运行新意图holdout。",
    )
    return parser.parse_args()


def _print_profile(result: IntentProfileResult) -> None:
    """打印比单一准确率更重要的四分类和安全指标。"""

    gate = (
        "REF"
        if result.classifier_kind == "keyword_baseline"
        else ("PASS" if result.quality_gate_passed else "FAIL")
    )
    print(
        f"{result.profile_id:<31} "
        f"Acc={result.overall_accuracy:.3f}  MacroF1={result.macro_f1:.3f}  "
        f"HumanRecall={result.human_handoff_recall:.3f}  "
        f"UnsafeAuto={result.unsafe_auto_route_rate:.3f}  "
        f"FalseReturn={result.false_return_route_rate:.3f}  {gate}"
    )


async def _run() -> int:
    """按确认参数装配零费用基线或真实千问客户端。"""

    args = _parse_args()
    if args.confirm_holdout and not args.confirm_paid_api:
        raise ValueError("运行holdout时必须同时提供 --confirm-paid-api")
    config = load_intent_experiment_config(CONFIG_PATH)
    qwen_client: LangChainIntentClassificationClient | None = None
    if args.confirm_paid_api:
        # Settings 自动读取项目根目录.env；模型名由版本化实验配置覆盖。
        settings = Settings(
            llm_backend="openai_compatible",
            llm_model=config.candidate_model,
        )
        # create_chat_model 复用项目现有千问OpenAI兼容配置、超时和重试。
        # v2已经通过一次性锁定集，默认客户端现在与生产LangGraph使用同一晋级提示。
        qwen_client = LangChainIntentClassificationClient(create_chat_model(settings))
    report = await run_intent_classification_experiment(
        config,
        qwen_client=qwen_client,
        include_holdout=args.confirm_holdout,
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== ServiceOps 第32步：意图分类与范围漂移实验 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"提示指纹匹配：{report.prompt_matches_config}")
    print(f"真实开发计划调用：{report.planned_development_chat_calls} 次")
    print(f"锁定集增量计划调用：{report.planned_holdout_chat_calls} 次")
    print("\n关键词规则基线：")
    _print_profile(report.keyword_development_baseline)
    if not report.qwen_development_candidates and report.qwen_holdout_candidate is None:
        print("\n千问候选：未运行（当前命令零费用）")
        print(f"基线失败样本：{report.keyword_development_baseline.failed_case_ids}")
        print(f"报告：{REPORT_PATH}")
        return 0
    if report.qwen_development_candidates:
        print("\n千问阈值候选：")
        for candidate in report.qwen_development_candidates:
            _print_profile(candidate)
        print(f"\n开发优胜候选：{report.selected_profile_id}")
        print(f"冻结候选匹配：{report.frozen_profile_matches_selection}")
        print(f"实际成功聊天调用：{report.successful_chat_calls} 次")
    if report.qwen_holdout_candidate is None:
        print("意图锁定集：未运行")
        print(f"报告：{REPORT_PATH}")
        return 0 if report.frozen_profile_matches_selection else 1
    print("\n一次性意图锁定结果：")
    print(f"冻结候选：{report.selected_profile_id}")
    print(f"实际锁定聊天调用：{report.successful_chat_calls} 次")
    _print_profile(report.qwen_holdout_candidate)
    print(f"报告：{REPORT_PATH}")
    return 0 if report.qwen_holdout_candidate.quality_gate_passed else 1


def main() -> int:
    """为普通同步命令行入口启动并关闭异步事件循环。"""

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
