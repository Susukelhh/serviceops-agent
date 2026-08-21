"""第十四步示例：重复运行真实千问候选，并与确定性离线基线比较。

该脚本会产生真实模型调用和可能的 API 费用，因此不会仅凭“右键运行”开始实验。
必须在 PyCharm Run Configuration 的 Parameters 中显式加入：

    --confirm-paid-api

默认按版本化配置重复三轮；当前 13 条黄金集参考路径约产生 24 次聊天请求/轮。
服务商重试或模型错误路由可能改变实际调用数，运行前请确认账户额度。
"""

# argparse 解析数据集、实验配置、轮数、报告路径和付费确认开关。
import argparse

# asyncio 在确认付费调用后运行异步 LangGraph 多轮实验。
import asyncio

# os 只为当前子进程固定候选 profile，不会修改项目根目录 .env。
import os


def _parse_arguments() -> argparse.Namespace:
    """解析候选实验参数；本函数不会导入项目或访问模型服务。"""

    # description 会显示在 PyCharm Terminal 的 --help 输出中。
    parser = argparse.ArgumentParser(
        description="比较 ServiceOps Agent 离线基线和真实千问候选的多轮稳定性",
    )
    # 端到端黄金集与第十三步 CI 使用同一版本，保证比较口径一致。
    parser.add_argument(
        "--dataset",
        default="data/evaluation/agent_end_to_end_cases.json",
        help="端到端黄金数据集路径，默认相对于项目根目录",
    )
    # 实验配置保存 profile、默认轮数和晋级阈值。
    parser.add_argument(
        "--experiment-config",
        default="data/evaluation/qwen_candidate_experiment.json",
        help="候选实验配置路径，默认相对于项目根目录",
    )
    # None 表示沿用配置文件；显式值仍会经过 1..10 的 Pydantic 校验。
    parser.add_argument(
        "--trials",
        type=int,
        default=None,
        help="覆盖候选重复轮数，允许 1 到 10；缺省使用配置文件",
    )
    # 运行报告进入已被 .gitignore 排除的 runtime 目录，避免提交机器/模型波动数据。
    parser.add_argument(
        "--report",
        default="data/runtime/qwen_candidate_experiment_report.json",
        help="候选实验 JSON 报告路径，默认相对于项目根目录",
    )
    # 没有该显式开关时脚本在任何项目导入和网络调用前以退出码 2 停止。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认本次运行允许调用真实模型并可能产生费用",
    )
    # 分析失败候选时可保留零退出码；GitHub 晋级工作流不应使用该参数。
    parser.add_argument(
        "--allow-promotion-failure",
        action="store_true",
        help="晋级门失败仍返回退出码 0，仅用于本地失败分析",
    )
    # 返回 argparse 的简单 Namespace，不在解析阶段读取 .env。
    return parser.parse_args()


def _fix_candidate_process_profile() -> None:
    """在导入项目模块前固定本次真实候选的非密钥运行配置。"""

    # 分类使用真实 OpenAI 兼容聊天模型。
    os.environ["SERVICEOPS_LLM_BACKEND"] = "openai_compatible"
    # 订单循环使用真实结构化规划器。
    os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "llm"
    # Embedding 故意保持 Hash，单独控制聊天模型这一实验变量。
    os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
    # FAQ 最终草稿使用真实模型，但仍经过确定性引用安全门。
    os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "llm"
    # Qdrant、Checkpoint 和副作用仓库全部留在当前进程内存。
    os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"
    os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
    # 候选实验不需要把大量 Span 打到 Console 或外部 Collector。
    os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "false"


async def _run_experiment(arguments: argparse.Namespace) -> bool:
    """付费确认后加载配置、运行基线与候选、写入脱敏报告。"""

    # 延迟导入确保未确认付费时不会触发项目全局装配或任何外部客户端初始化。
    from serviceops_agent.config.paths import resolve_project_path
    from serviceops_agent.config.settings import Settings
    from serviceops_agent.evaluation import (
        build_qwen_candidate_evaluation_target,
        estimate_planned_qwen_chat_calls,
        load_agent_evaluation_dataset,
        load_candidate_experiment_config,
        override_candidate_trial_count,
        run_candidate_experiment,
    )

    # 先校验黄金集与实验配置，坏标签或坏门槛不会消耗真实模型额度。
    dataset = load_agent_evaluation_dataset(str(arguments.dataset))
    raw_config = load_candidate_experiment_config(str(arguments.experiment_config))
    config = override_candidate_trial_count(raw_config, arguments.trials)
    # Settings 从项目根目录 .env 读取模型 ID、SecretStr API Key 和 Base URL。
    source_settings = Settings()
    # 参考调用数帮助用户在第一条真实请求发出前再次确认成本规模。
    planned_calls_per_trial = estimate_planned_qwen_chat_calls(dataset)
    print("=== 即将运行真实千问候选实验 ===")
    # 版本变化表示提示、目标配置或晋级口径发生变化，旧报告不能直接混入同一聚合。
    print(f"实验版本：{config.version}")
    print(f"模型：{source_settings.llm_model}")
    print(f"候选轮数：{config.trials}")
    print(f"参考聊天调用：约 {planned_calls_per_trial} 次/轮")
    print(f"参考总调用：约 {planned_calls_per_trial * config.trials} 次")
    print("说明：SDK 重试或错误路由可能改变实际调用数。")

    # 闭包确保每轮使用同一非密钥配置快照，同时让构造器重新创建所有状态资源。
    def build_candidate_target():
        """为单轮候选返回全新图和仓库。"""

        return build_qwen_candidate_evaluation_target(source_settings)

    # 先自动运行严格离线基线，再依次运行真实候选；不并发可避免瞬时放大限流与费用。
    summary = await run_candidate_experiment(
        dataset=dataset,
        config=config,
        candidate_model=source_settings.llm_model,
        candidate_target_factory=build_candidate_target,
    )

    # 报告目录可能尚不存在，写入前幂等创建。
    report_path = resolve_project_path(str(arguments.report))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic 序列化只包含模型 ID、指标和合成样本结果，不包含 API Key 与 Base URL。
    report_path.write_text(
        summary.model_dump_json(indent=2),
        encoding="utf-8",
    )

    # 控制台展示最重要的多轮与晋级证据，不打印完整模型答案。
    print("\n=== ServiceOps Agent 千问候选实验结果 ===")
    print(
        "离线基线："
        + ("PASS" if summary.baseline_summary.quality_gate_passed else "FAIL")
    )
    for trial_number, trial in enumerate(summary.candidate_trials, start=1):
        print(
            f"候选第 {trial_number} 轮："
            f"{trial.passed_cases}/{trial.total_cases}，"
            f"整体 {trial.overall_pass_rate:.2%}，"
            f"安全 {trial.safety_invariant_accuracy:.2%}"
        )
    print(f"候选平均整体通过率：{summary.mean_overall_pass_rate:.2%}")
    print(f"候选最差轮整体通过率：{summary.worst_trial_overall_pass_rate:.2%}")
    print(f"全轮稳定样本率：{summary.fully_stable_case_rate:.2%}")
    print(f"平均安全不变量准确率：{summary.mean_safety_invariant_accuracy:.2%}")
    print(f"晋级门：{'PASS' if summary.promotion_gate_passed else 'FAIL'}")
    print(f"报告：{report_path}")

    # 失败时同时展示聚合门码和不稳定 case，便于下一步提示/数据修复。
    if summary.promotion_gate_failures:
        print("晋级门失败：" + ", ".join(summary.promotion_gate_failures))
    unstable_cases = [result for result in summary.case_stability if not result.fully_stable]
    if unstable_cases:
        print("\n=== 非全轮稳定样本 ===")
        for result in unstable_cases:
            violations = ", ".join(result.observed_violations) or "intermittent_failure"
            print(
                f"{result.case_id}: {result.passed_trials}/{result.total_trials}，"
                f"{violations}"
            )
    # 返回晋级结论，由 main 转换成自动化系统可以识别的退出码。
    return summary.promotion_gate_passed


def main() -> int:
    """执行付费保护、候选 profile 固定和异步实验。"""

    # 参数解析不会读取项目配置或创建模型客户端。
    arguments = _parse_arguments()
    # 显式保护避免用户第一次在 PyCharm 误点运行就产生真实调用费用。
    if not bool(arguments.confirm_paid_api):
        print("未开始真实模型实验：请确认额度后添加 --confirm-paid-api。")
        print("当前没有导入项目评测模块，也没有发出任何千问请求。")
        return 2
    # 只有确认后才固定真实候选环境并导入项目。
    _fix_candidate_process_profile()
    # asyncio.run 创建并最终关闭本次实验事件循环。
    promotion_passed = asyncio.run(_run_experiment(arguments))
    # GitHub 手工晋级工作流依赖非零退出码阻断失败候选。
    if not promotion_passed and not bool(arguments.allow_promotion_failure):
        return 1
    return 0


# 只有直接从 PyCharm 或命令行运行时才执行，导入文件不会开始付费实验。
if __name__ == "__main__":
    raise SystemExit(main())
