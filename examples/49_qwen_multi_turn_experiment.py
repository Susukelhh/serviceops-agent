"""第49步：手工运行真实千问多轮候选实验。"""

import argparse
import asyncio
import os


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行真实千问共享会话重复评测")
    parser.add_argument(
        "--dataset",
        default="data/evaluation/conversation_stability_cases.json",
    )
    parser.add_argument(
        "--experiment-config",
        default="data/evaluation/qwen_multi_turn_experiment.json",
    )
    parser.add_argument(
        "--report",
        default="data/runtime/qwen_multi_turn_experiment_report.json",
    )
    parser.add_argument("--trials", type=int)
    parser.add_argument("--confirm-paid-api", action="store_true")
    parser.add_argument("--allow-promotion-failure", action="store_true")
    return parser.parse_args()


def _fix_candidate_profile() -> None:
    os.environ["SERVICEOPS_ENVIRONMENT"] = "test"
    os.environ["SERVICEOPS_LLM_BACKEND"] = "openai_compatible"
    os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "llm"
    os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
    os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "llm"
    os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"
    os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
    os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "false"


async def _run(arguments: argparse.Namespace) -> bool:
    from serviceops_agent.config.paths import resolve_project_path
    from serviceops_agent.config.settings import Settings
    from serviceops_agent.evaluation import (
        build_qwen_candidate_evaluation_target,
        enforce_qwen_multi_turn_budget,
        estimate_qwen_multi_turn_chat_calls,
        load_conversation_stability_dataset,
        load_qwen_multi_turn_config,
        override_qwen_multi_turn_trials,
        run_qwen_multi_turn_experiment,
    )

    dataset = load_conversation_stability_dataset(str(arguments.dataset))
    config = override_qwen_multi_turn_trials(
        load_qwen_multi_turn_config(str(arguments.experiment_config)),
        arguments.trials,
    )
    planned_total = enforce_qwen_multi_turn_budget(dataset, config)
    calls_per_trial = estimate_qwen_multi_turn_chat_calls(dataset, config)
    settings = Settings()
    print("=== 即将运行真实千问多轮候选实验 ===")
    print(f"实验：{config.experiment_id} v{config.version}")
    print(f"模型：{settings.llm_model}")
    print(f"候选轮数：{config.trials}")
    print(f"参考调用：{calls_per_trial} 次/轮，共 {planned_total} 次")
    print(f"预算硬上限：{config.max_planned_chat_calls} 次")

    def target_factory():
        return build_qwen_candidate_evaluation_target(settings)

    report = await run_qwen_multi_turn_experiment(
        dataset=dataset,
        config=config,
        candidate_model=settings.llm_model,
        target_factory=target_factory,
    )
    report_path = resolve_project_path(str(arguments.report))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print("\n=== 千问多轮候选结果 ===")
    print(f"平均轮次通过率：{report.mean_turn_pass_rate:.2%}")
    print(f"最差场景通过率：{report.worst_scenario_pass_rate:.2%}")
    print(f"全轮稳定场景率：{report.fully_stable_scenario_rate:.2%}")
    print(f"跨轮波动场景率：{report.cross_trial_instability_rate:.2%}")
    print(f"安全准确率：{report.mean_safety_accuracy:.2%}")
    print(f"晋级门：{'PASS' if report.promotion_gate_passed else 'FAIL'}")
    print(f"报告：{report_path}")
    if report.promotion_gate_failures:
        print("失败：" + ", ".join(report.promotion_gate_failures))
    return report.promotion_gate_passed


def main() -> int:
    arguments = _parse_arguments()
    if not bool(arguments.confirm_paid_api):
        print("未开始真实模型实验：请确认额度后添加 --confirm-paid-api。")
        return 2
    _fix_candidate_profile()
    passed = asyncio.run(_run(arguments))
    if passed or bool(arguments.allow_promotion_failure):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
