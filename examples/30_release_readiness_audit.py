"""第30步：生成GitHub公开、简历证据和本地质量门发布验收报告。"""

# argparse提供是否运行耗时质量门的显式开关。
import argparse

# json把Pydantic报告保存为UTF-8文件。
import json

# Path声明报告路径类型。
from pathlib import Path

# PROJECT_ROOT保证PyCharm从任意工作目录启动都审计同一项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# 发布验收器只执行本地检查，不提交Git或访问远程仓库。
from serviceops_agent.portfolio import ReleaseReadinessCheck, run_release_readiness_audit

# REPORT_PATH属于runtime，不会进入Git提交或泄露本机绝对路径。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/release_readiness_report.json"


def _parse_args() -> argparse.Namespace:
    """解析是否额外运行Ruff、Mypy、Pytest和依赖锁检查。"""

    # parser默认只执行快速发布安全审计。
    parser = argparse.ArgumentParser(description="运行ServiceOps Agent发布验收。")
    # 质量门大约需要十几秒，因此由用户显式开启。
    parser.add_argument(
        "--run-quality-gates",
        action="store_true",
        help="同时运行Ruff、Mypy、全部Pytest和uv lock检查。",
    )
    # 返回解析后的命名空间。
    return parser.parse_args()


def _status_icon(check: ReleaseReadinessCheck) -> str:
    """把三种状态转换成容易阅读的控制台标记。"""

    # icons不依赖终端颜色，复制到聊天仍能辨认。
    icons = {"pass": "PASS", "warning": "WARN", "blocker": "BLOCK"}
    # Pydantic已限制status只能是三个键之一。
    return icons[check.status]


def main() -> int:
    """运行审计、保存报告并在存在阻断项时返回退出码1。"""

    # 读取可选质量门开关。
    args = _parse_args()
    # 执行完全本地的只读检查；只有runtime报告文件会在后面写入。
    report = run_release_readiness_audit(
        PROJECT_ROOT,
        run_quality_gates=args.run_quality_gates,
    )
    # 新环境可能尚无runtime目录。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 保存完整结构化结果；绝对项目路径只留在被忽略的本地报告中。
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # 打印整体摘要。
    print("=== ServiceOps Agent 第30步：发布验收 ===")
    print(f"Git公开候选文件：{report.candidate_public_file_count}个")
    print(
        f"PASS={report.passed_checks}，WARN={report.warning_checks}，"
        f"BLOCK={report.blocker_checks}"
    )
    # 按报告顺序展示每项结论。
    for check in report.checks:
        # 第一行便于快速扫描。
        print(f"\n[{_status_icon(check)}] {check.title}")
        # detail只含脱敏证据。
        print(check.detail)
        # 有修复建议时另起一行。
        if check.remediation:
            # 明确这是下一步动作。
            print(f"下一步：{check.remediation}")
    # 最后再次给出是否适合推送的明确结论。
    print(
        "\n公开推送结论："
        + ("READY" if report.ready_for_public_push else "NOT READY")
    )
    print(f"本地报告：{REPORT_PATH}")
    # 阻断项未清零时返回1，warning不阻断代码完成度。
    return 0 if report.ready_for_public_push else 1


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit让运行窗口显示发布门状态。
    raise SystemExit(main())
