"""第33步：从 Nginx 公网入口运行新版端到端黑盒盲测。

本脚本不会读取或调用千问 API。它只测试已经运行的部署，因此 Docker 默认离线模式费用为零；
若部署者自己把目标服务切到真实模型，费用发生在被测服务端，而不是脚本内部。
"""

# argparse 接收目标地址和可选报告路径；asyncio 驱动异步 HTTP 评测器。
import argparse
import asyncio

# json 保存可审计、无 Token/线程/问题原文的聚合报告。
import json

# Path 为版本控制配置和 runtime 报告提供明确类型。
from pathlib import Path

# AsyncClient 复用连接池，通过统一网关测试真实部署而不是内部 Python 函数。
from httpx import AsyncClient

# PROJECT_ROOT 让 PyCharm Working directory 不影响默认路径。
from serviceops_agent.config.paths import PROJECT_ROOT

# 公共评测包提供配置加载、黑盒执行和强类型结果。
from serviceops_agent.evaluation import (
    PublicDemoBlindReport,
    evaluate_public_demo_blind_suite,
    load_public_demo_blind_config,
)

# CONFIG_PATH 是冻结的新措辞、期望、版本和质量门。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/public_demo_end_to_end_blind_test.json"
)
# REPORT_PATH 位于已忽略的 runtime，避免每次耗时变化污染 Git。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/public_demo_blind_report.json"


def _parse_arguments() -> argparse.Namespace:
    """解析统一入口、连接超时和本机报告路径。"""

    # 默认地址就是 Docker Compose 暴露的 Nginx，而不是某一只 FastAPI。
    parser = argparse.ArgumentParser(
        description="运行 ServiceOps 公网 Demo 新版端到端黑盒盲测。"
    )
    # base-url 可替换成未来 HTTPS 公网域名；末尾斜杠由 HTTPX 自动处理。
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="被测统一入口，默认是本机 Docker Nginx。",
    )
    # timeout 是每次 HTTP 请求上限，不改变服务端自己的模型和图超时。
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="单次黑盒 HTTP 请求超时，默认30秒。",
    )
    # 允许面试前把报告另存到指定项目路径。
    parser.add_argument(
        "--report",
        default=str(REPORT_PATH),
        help="脱敏 JSON 报告输出路径。",
    )
    # 返回 argparse 已完成基础类型转换的结果。
    return parser.parse_args()


def _print_report(report: PublicDemoBlindReport, report_path: Path) -> None:
    """打印四维指标、实例覆盖、P95 和失败原因码。"""

    print("=== ServiceOps 第33步：新版端到端黑盒盲测 ===")
    print(f"测试套件：{report.suite_id} v{report.suite_version}")
    print(f"部署运行模式：{report.runtime_mode}")
    print(f"检查通过：{report.passed_checks}/{report.total_checks}")
    print(f"整体通过率：{report.overall_pass_rate:.2%}")
    print(f"可用性：{report.availability_accuracy:.2%}")
    print(f"四路径业务契约：{report.business_accuracy:.2%}")
    print(f"安全与隔离：{report.safety_accuracy:.2%}")
    print(f"暂停恢复与审计：{report.recovery_accuracy:.2%}")
    print(f"实际命中实例：{', '.join(report.distinct_instances) or '未观测到'}")
    print(f"P95 黑盒耗时：{report.p95_duration_ms:.2f} ms")
    print(f"质量门：{'PASS' if report.quality_gate_passed else 'FAIL'}")
    # 只打印失败项和稳定规则码，成功项保留在 JSON 报告中。
    failed_results = [result for result in report.results if not result.passed]
    if failed_results:
        print("\n=== 失败检查 ===")
        for result in failed_results:
            print(f"- {result.check_id}：{', '.join(result.violations)}")
    if report.quality_gate_failures:
        print("聚合门失败：" + ", ".join(report.quality_gate_failures))
    print(f"报告：{report_path}")


async def _run() -> int:
    """执行冻结配置、保存脱敏报告并返回可供 CI 使用的退出码。"""

    arguments = _parse_arguments()
    # 超时必须为正，避免 HTTPX 获得无意义或无限的边界。
    if arguments.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds 必须大于0")
    # 在任何网络请求前完成数据集 Schema、覆盖类型和标签一致性校验。
    config = load_public_demo_blind_config(str(CONFIG_PATH))
    # base_url 只决定目标入口；所有路径仍由评测器固定，不能注入任意外部 URL。
    async with AsyncClient(
        base_url=str(arguments.base_url).rstrip("/"),
        timeout=float(arguments.timeout_seconds),
        follow_redirects=False,
    ) as client:
        report = await evaluate_public_demo_blind_suite(client, config)
    # 报告路径允许绝对地址；相对地址明确锚定项目根目录。
    raw_report_path = Path(str(arguments.report))
    report_path = (
        raw_report_path
        if raw_report_path.is_absolute()
        else PROJECT_ROOT / raw_report_path
    )
    # runtime 目录首次使用时可能不存在，因此幂等创建父目录。
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # 报告只包含低敏观测、分数和规则码，不保存 JWT、session_id、thread_id 或问题原文。
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_report(report, report_path)
    # 质量门失败使用标准非零退出码，便于 PyCharm 和 CI 明确标红。
    return 0 if report.quality_gate_passed else 1


def main() -> int:
    """为 PyCharm 普通运行配置创建并关闭事件循环。"""

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
