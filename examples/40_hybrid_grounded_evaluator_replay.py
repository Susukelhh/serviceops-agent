"""第40步：零费用回放“确定性红线 + 语义完整性Judge”分层评测器。"""

# json保存公开脱敏集成结果。
import json

# os提供刷盘、原子替换和独占硬链接。
import os

# Path声明配置、runtime报告和版本化结果位置。
from pathlib import Path

# uuid4给临时文件生成不可碰撞名称。
from uuid import uuid4

# PROJECT_ROOT保证PyCharm与PowerShell从任意目录启动都能定位文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第40步只调用纯本地配置loader和分层裁决器。
from serviceops_agent.evaluation import (
    HybridGroundedEvaluationReport,
    load_hybrid_grounded_evaluator_config,
    run_hybrid_grounded_evaluation_replay,
)

# CONFIG_PATH冻结三个公开来源SHA、优先级规则和80%门。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/hybrid_grounded_evaluator_v1.json"
)
# REPORT_PATH是可重复生成的本机runtime报告。
REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/hybrid_grounded_evaluator_replay_report.json"
)
# RESULT_PATH是版本化公开证据；相同来源重复运行必须得到逐字节相同内容。
RESULT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/hybrid_grounded_evaluator_v1_replay_result.json"
)


def _serialized(payload: object) -> str:
    """把公开报告转换成跨机器稳定的UTF-8 JSON文本。"""

    # ensure_ascii=False保留中文字段值可读性；indent方便代码审查。
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _write_atomic_replace(path: Path, content: str) -> None:
    """完整落盘后原子替换允许更新的runtime报告。"""

    # 新克隆可能没有runtime目录。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证替换不跨磁盘。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    try:
        # x模式防止极小概率临时文件名碰撞。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # content已经在内存完成序列化。
            handle.write(content)
            # 刷新Python缓冲区。
            handle.flush()
            # 要求操作系统完成刷盘。
            os.fsync(handle.fileno())
        # 目标只会看到旧完整版本或新完整版本。
        os.replace(temporary_path, path)
    finally:
        # 任意异常都清理partial文件。
        temporary_path.unlink(missing_ok=True)


def _publish_idempotent_result(path: Path, content: str) -> None:
    """首次独占发布；后续只能接受逐字节完全相同的确定性回放。"""

    # 版本化结果目录在新克隆中可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 已存在时不覆盖，只比较确定性输出。
    if path.is_file():
        # 不同内容表示来源、代码或环境发生了未版本化变化。
        if path.read_text(encoding="utf-8") != content:
            # 拒绝静默覆盖历史证据。
            raise RuntimeError("第40步既有结果与当前确定性回放不一致")
        # 相同内容表示幂等复跑成功。
        return
    # 新结果先写同目录唯一临时文件。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    try:
        # x模式独占创建临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整公开报告。
            handle.write(content)
            # 刷新Python缓冲区。
            handle.flush()
            # 发布前完成操作系统刷盘。
            os.fsync(handle.fileno())
        # 硬链接在竞争进程先发布时原子失败，不会覆盖。
        try:
            # 发布最终文件名。
            os.link(temporary_path, path)
        except FileExistsError:
            # 并发进程发布的内容也必须逐字节一致。
            if path.read_text(encoding="utf-8") != content:
                # 不一致时保留先发布结果并失败。
                raise RuntimeError("并发第40步回放产生不一致结果") from None
    finally:
        # 成功或异常都删除临时别名。
        temporary_path.unlink(missing_ok=True)


def _print_report(report: HybridGroundedEvaluationReport) -> None:
    """展示正式原结果、分层回放结果和零费用边界。"""

    # 缩短局部变量，后续只读取强类型Summary。
    summary = report.summary
    # 明确这不是新盲测。
    print("=== ServiceOps 第40步：混合评测器已揭晓集成回放 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"评分规则：{report.evaluator_version}")
    print(f"评测器指纹：{report.evaluator_fingerprint}")
    print("运行性质：REVEALED_INTEGRATION_REPLAY（不是新盲测）")
    # 原正式分子保持20，不篡改第38步证据。
    print(
        f"第38步确定性正式结果：{summary.deterministic_passed_cases}/"
        f"{summary.total_cases}"
    )
    # 只展示合法语义升级数量。
    print(f"纯完整性争议经Judge升级：{summary.semantic_override_cases}条")
    # 最终回放仍使用同一个端到端有据回答成功率。
    print(
        "分层评测回放成功率："
        f"{summary.final_passed_cases}/{summary.total_cases} = "
        f"{summary.grounded_answer_success_rate:.2%}"
    )
    # 红线即使存在也不会被Judge清空。
    print(f"保留确定性红线：{len(summary.red_line_case_ids)}条")
    # Gate只代表已揭晓集成回放。
    print(f"集成质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 该步骤不创建任何收费客户端。
    print(
        "API调用：Embedding 0次；Agent生成 0次；Judge 0次（复用冻结结论）"
    )
    # 再次提醒不能把结果写成新盲测100%。
    print("说明：该结果只验证裁决优先级和集成逻辑，不能替换第38步66.67%。")


def main() -> int:
    """执行纯本地来源校验、分层裁决和幂等结果发布。"""

    # 加载不含私有正文的公开配置。
    config = load_hybrid_grounded_evaluator_config(CONFIG_PATH)
    # 运行纯同步、零网络裁决。
    report = run_hybrid_grounded_evaluation_replay(config)
    # 在内存完成稳定序列化。
    content = _serialized(report.model_dump(mode="json"))
    # runtime报告允许原子更新。
    _write_atomic_replace(REPORT_PATH, content)
    # 版本化结果首次发布，后续只能幂等复跑。
    _publish_idempotent_result(RESULT_PATH, content)
    # 展示用户可读结论。
    _print_report(report)
    # 显示两个脱敏文件位置。
    print(f"版本化结果：{RESULT_PATH}")
    print(f"本机报告：{REPORT_PATH}")
    # 集成Gate决定退出码。
    return 0 if report.summary.quality_gate_passed else 1


if __name__ == "__main__":
    # 把质量门退出码交给PowerShell和PyCharm。
    raise SystemExit(main())
