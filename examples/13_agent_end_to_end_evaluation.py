"""第十三步示例：运行完整 LangGraph 的离线 Agent 回归评测与质量门。

在 PyCharm 中直接运行本文件，或在项目根目录执行：

    uv run python examples/13_agent_end_to_end_evaluation.py

脚本会固定使用 mock/hash/extractive/deterministic/:memory:，不会调用千问或消耗额度。
质量门失败时进程退出码为 1，因此同一脚本可以直接放进 CI。
"""

# argparse 支持替换数据集或报告路径；asyncio 运行异步 LangGraph 评测器。
import argparse
import asyncio

# os 必须在导入项目模块前固定离线环境变量，覆盖本机 .env 的真实模型设置。
import os

# 以下设置只影响当前示例子进程，不会修改 D:\serviceops-agent\.env 文件。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
os.environ["SERVICEOPS_AGENT_PLANNER_BACKEND"] = "deterministic"
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"
os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "false"

# resolve_project_path 让命令行和 PyCharm 使用相同项目相对路径语义。
from serviceops_agent.config.paths import resolve_project_path

# 评测包提供数据加载、隔离目标构造和整图指标计算。
from serviceops_agent.evaluation import (
    build_offline_agent_evaluation_target,
    evaluate_agent_dataset,
    load_agent_evaluation_dataset,
)


def _parse_arguments() -> argparse.Namespace:
    """解析数据集、报告输出和教学调试开关。"""

    # description 会显示在 PyCharm Terminal 的 --help 中。
    parser = argparse.ArgumentParser(
        description="运行 ServiceOps Agent 完全离线端到端评测",
    )
    # 标准数据集受 Git 版本控制；调用方也可传入另一个项目相对路径。
    parser.add_argument(
        "--dataset",
        default="data/evaluation/agent_end_to_end_cases.json",
        help="评测数据集路径，默认相对于项目根目录",
    )
    # 报告写到 .gitignore 覆盖的 runtime 目录，避免把机器耗时变化提交到 Git。
    parser.add_argument(
        "--report",
        default="data/runtime/agent_end_to_end_report.json",
        help="JSON 报告输出路径，默认相对于项目根目录",
    )
    # 本地调试坏样本时可以保留零退出码；CI 不应添加该参数。
    parser.add_argument(
        "--allow-gate-failure",
        action="store_true",
        help="即使质量门失败也返回退出码 0，仅用于本地分析",
    )
    # 返回 argparse 生成的简单 Namespace。
    return parser.parse_args()


async def _run_evaluation(dataset_path: str, report_path: str) -> bool:
    """加载数据、构造隔离目标、运行评测并保存脱敏 JSON 报告。"""

    # 数据集先通过 Pydantic 校验；重复 ID 或矛盾标签会在执行模型前失败。
    dataset = load_agent_evaluation_dataset(dataset_path)
    # 每次运行新建 Qdrant 内存索引、Checkpointer 和退货仓库，避免历史状态污染。
    graph, return_repository = build_offline_agent_evaluation_target()
    # 运行全部人工标注样本，得到四维指标和逐样本违反规则。
    summary = await evaluate_agent_dataset(
        graph,
        return_repository,
        dataset,
    )

    # 报告目录可能尚不存在，运行前幂等创建。
    resolved_report_path = resolve_project_path(report_path)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic 负责把枚举和嵌套结果序列化为 UTF-8 JSON；报告不含完整输入数据集。
    resolved_report_path.write_text(
        summary.model_dump_json(indent=2),
        encoding="utf-8",
    )

    # 控制台先给求职演示最重要的聚合结论。
    print("=== ServiceOps Agent 端到端离线评测 ===")
    print(f"数据集：{summary.dataset_id} v{summary.dataset_version}")
    print(f"目标配置：{summary.target_profile}")
    print(f"样本通过：{summary.passed_cases}/{summary.total_cases}")
    print(f"整体通过率：{summary.overall_pass_rate:.2%}")
    print(f"意图路由准确率：{summary.routing_accuracy:.2%}")
    print(f"工具轨迹准确率：{summary.tool_trajectory_accuracy:.2%}")
    print(f"响应契约准确率：{summary.response_contract_accuracy:.2%}")
    print(f"安全不变量准确率：{summary.safety_invariant_accuracy:.2%}")
    print(f"P95 整图耗时：{summary.p95_duration_ms:.2f} ms")
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    print(f"报告：{resolved_report_path}")

    # 只有失败样本输出稳定规则码，避免成功场景控制台过长。
    failed_results = [result for result in summary.results if not result.passed]
    if failed_results:
        print("\n=== 失败样本 ===")
        for result in failed_results:
            print(f"{result.case_id}: {', '.join(result.violations)}")
    # 聚合门失败可能来自 P95，即使所有单条功能样本都通过也要单独展示。
    if summary.quality_gate_failures:
        print("聚合门失败：" + ", ".join(summary.quality_gate_failures))

    # 返回布尔结论供同步 main 决定 CI 退出码。
    return summary.quality_gate_passed


def main() -> int:
    """运行异步评测，并把质量门结果转换为标准进程退出码。"""

    arguments = _parse_arguments()
    # asyncio.run 负责创建、运行并关闭本示例事件循环。
    gate_passed = asyncio.run(
        _run_evaluation(
            dataset_path=str(arguments.dataset),
            report_path=str(arguments.report),
        )
    )
    # CI 中质量门失败必须非零；本地显式 allow 时只展示失败而不阻断进程。
    if not gate_passed and not bool(arguments.allow_gate_failure):
        return 1
    return 0


# 只有直接从 PyCharm 或命令行运行时才执行；导入本文件不会自动开始评测。
if __name__ == "__main__":
    raise SystemExit(main())
