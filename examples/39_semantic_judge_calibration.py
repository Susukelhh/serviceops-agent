"""第39步：校准“确定性红线 + 语义完整性Judge”的混合评测方案。

默认只展示公开计划；确认私有回归后只加载20条校准项；再确认付费才调用20次Judge。该入口不会重新调用
Embedding，也不会重新生成Agent答案。
"""

# argparse解析私有数据与付费模型两把钥匙。
import argparse

# asyncio启动异步结构化Judge调用。
import asyncio

# json把脱敏报告保存为UTF-8文件。
import json

# os提供刷盘、原子替换和独占硬链接。
import os

# Path统一声明配置、runtime报告和首次校准结果位置。
from pathlib import Path

# uuid4为临时文件生成不可碰撞名称。
from uuid import uuid4

# PROJECT_ROOT保证PyCharm和PowerShell从任意目录启动都能定位项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings只在用户明确确认付费后读取.env。
from serviceops_agent.config.settings import Settings

# 第39步复用公开强类型配置、报告、指纹和校准运行器。
from serviceops_agent.evaluation import (
    SemanticJudgeCalibrationReport,
    load_semantic_judge_calibration_config,
    run_semantic_judge_calibration,
)

# CONFIG_PATH只含来源SHA、Judge参数和质量门，不含私有问题或答案。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/semantic_judge_calibration_v1.json"
)
# REPORT_PATH保存本机脱敏运行报告；runtime目录不会进入Git。
REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/semantic_judge_calibration_report.json"
)
# FROZEN_RESULT_PATH只允许首次真实校准创建一次。
FROZEN_RESULT_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/results/semantic_judge_calibration_v1_result.json"
)


def _parse_args() -> argparse.Namespace:
    """解析读取私有回归与真实付费Judge两把钥匙。"""

    # 默认不带参数时费用为零，也不会扫描私有诊断目录。
    parser = argparse.ArgumentParser(description="运行第39步语义Judge校准实验。")
    # 第一把钥匙允许加载第38步已揭晓私有诊断。
    parser.add_argument(
        "--confirm-private-regression",
        action="store_true",
        help="确认读取第38步本机私有回归诊断。",
    )
    # 第二把钥匙允许调用20次真实千问Judge。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认调用真实千问语义Judge。",
    )
    # 返回标准参数对象。
    return parser.parse_args()


def _validate_args(
    args: argparse.Namespace,
    *,
    frozen_result_exists: bool | None = None,
) -> None:
    """在读取私有文件、.env或创建模型前验证安全状态。"""

    # 测试可注入历史状态；真实运行直接检查首次结果路径。
    result_exists = (
        FROZEN_RESULT_PATH.is_file()
        if frozen_result_exists is None
        else frozen_result_exists
    )
    # 付费必须同时允许读取本机私有校准输入。
    if args.confirm_paid_api and not args.confirm_private_regression:
        # 在目录扫描和Key读取前失败。
        raise ValueError(
            "--confirm-paid-api必须同时提供--confirm-private-regression"
        )
    # 首次真实校准结果存在后不能反复运行挑选最好的一次。
    if args.confirm_paid_api and result_exists:
        # 新Judge候选必须建立新版本配置和结果文件。
        raise ValueError("第39步首次校准结果已存在，不能重复执行付费校准")


def _write_atomic_replace(path: Path, payload: object) -> None:
    """完整写好runtime报告后再原子替换旧报告。"""

    # 新克隆可能没有runtime目录。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证os.replace不跨磁盘。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 先在内存完成序列化，失败不会产生半份JSON。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式防止极小概率临时文件名碰撞。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整文本。
            handle.write(content)
            # 清空Python缓冲区。
            handle.flush()
            # 要求操作系统完成刷盘。
            os.fsync(handle.fileno())
        # 目标只会看到旧完整版本或新完整版本。
        os.replace(temporary_path, path)
    finally:
        # 任意异常都清理partial文件。
        temporary_path.unlink(missing_ok=True)


def _write_atomic_exclusive(path: Path, payload: object) -> None:
    """独占创建首次Judge校准结果，已有目标时绝不覆盖。"""

    # 公开结果目录在新克隆中可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件和目标保持同一文件系统。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 首次结果在内存中完整序列化。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式独占创建临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入脱敏聚合结果。
            handle.write(content)
            # 刷新Python缓冲区。
            handle.flush()
            # 发布前完成操作系统刷盘。
            os.fsync(handle.fileno())
        # 硬链接在目标存在时会原子失败，因此没有覆盖窗口。
        os.link(temporary_path, path)
    finally:
        # 成功后删除临时别名，异常也不保留partial文件。
        temporary_path.unlink(missing_ok=True)


def _public_result_payload(
    report: SemanticJudgeCalibrationReport,
) -> dict[str, object]:
    """提取不含问题、答案、证据和模型理由的首次公开结果。"""

    # 真实付费路径必须具有summary。
    summary = report.summary
    # 防止离线装载结果冒充Judge质量结论。
    if summary is None:
        # 固定错误不泄漏私有输入。
        raise RuntimeError("第39步真实Judge校准结果缺失")
    # 公开内容只保留身份、唯一指标和脱敏错项。
    return {
        "experiment_id": report.experiment_id,
        "experiment_version": report.experiment_version,
        "run_kind": "FIRST_SEMANTIC_JUDGE_CALIBRATION",
        "source_diagnostic_sha256": report.source_diagnostic_sha256,
        "judge_fingerprint": report.judge_fingerprint,
        "profile_id": summary.profile_id,
        "total_items": summary.total_items,
        "matched_items": summary.matched_items,
        "calibration_accuracy": summary.calibration_accuracy,
        "quality_gate_passed": summary.quality_gate_passed,
        "mismatched_items": [
            {
                "item_id": result.item_id,
                "case_id": result.case_id,
                "variant": result.variant,
                "expected_pass": result.expected_pass,
                "predicted_pass": result.predicted_pass,
                "decision": result.decision,
                "reason_code": result.reason_code,
            }
            for result in summary.results
            if not result.matched
        ],
        "contains_private_questions": False,
        "contains_agent_answers": False,
        "contains_evidence_text": False,
        "contains_judge_reason_text": False,
    }


async def _run(args: argparse.Namespace) -> int:
    """执行公开计划、零费用私有装载或首次真实Judge校准。"""

    # 公开配置不含任何私有正文。
    config = load_semantic_judge_calibration_config(CONFIG_PATH)
    # 只有付费路径才读取.env；默认构造无Key设置。
    settings = (
        Settings()
        if args.confirm_paid_api
        else Settings.model_construct(telemetry_enabled=False)
    )
    # 运行器内部再次验证两把钥匙和候选冻结指纹。
    report = await run_semantic_judge_calibration(
        config,
        runtime_settings=settings,
        confirm_private_regression=args.confirm_private_regression,
        confirm_paid_api=args.confirm_paid_api,
    )
    # runtime报告不含私有正文，可以安全完整替换。
    _write_atomic_replace(REPORT_PATH, report.model_dump(mode="json"))
    # 展示公开实验身份和费用上限。
    print("=== ServiceOps 第39步：语义完整性Judge校准 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"评分规则：{report.evaluator_version}")
    print(f"Judge指纹：{report.judge_fingerprint}")
    print(f"计划Judge调用：{report.planned_judge_calls}次")
    # 默认路径没有读取私有诊断。
    if report.private_items_loaded == 0:
        # 明确费用为零。
        print("\n私有校准项：未读取；千问Judge：未调用（费用0元）")
        print("下一步零费用装载参数：--confirm-private-regression")
        print(f"报告：{REPORT_PATH}")
        # 计划展示正常退出。
        return 0
    # 第一把钥匙已验证20条校准项。
    print(f"\n私有校准项已验证：{report.private_items_loaded}条")
    # 没有付费确认时停止在本机装载阶段。
    if report.summary is None:
        # 明确没有重新调用Embedding或Agent回答模型。
        print("千问Judge：未调用（费用0元；Embedding 0次；Agent生成0次）")
        print("真实校准需同时添加--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 私有装载成功。
        return 0
    # 展示唯一校准指标。
    summary = report.summary
    print(
        "语义Judge人工一致率："
        f"{summary.matched_items}/{summary.total_items} = "
        f"{summary.calibration_accuracy:.2%}"
    )
    # Gate只比较预先冻结的90%门槛。
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 只打印错项稳定ID与有限原因，不输出任何私有正文或brief_reason。
    for result in summary.results:
        # 匹配项无需逐条制造噪声。
        if result.matched:
            # 继续下一项。
            continue
        # 公开输出足以定位本机源记录。
        print(
            f"- {result.item_id}：expected={result.expected_pass}，"
            f"predicted={result.predicted_pass}，{result.reason_code}"
        )
    # 展示真实调用数并强调没有额外Embedding和Agent生成。
    print(
        f"实际Judge调用：{report.actual_judge_calls}次；"
        "Embedding：0次；Agent答案生成：0次"
    )
    # 首次结果独占写入，禁止重复运行挑选最好分数。
    _write_atomic_exclusive(FROZEN_RESULT_PATH, _public_result_payload(report))
    # 展示公开证据和本机报告路径。
    print(f"首次冻结结果：{FROZEN_RESULT_PATH}")
    print(f"报告：{REPORT_PATH}")
    # Judge是否达到校准门决定退出码。
    return 0 if summary.quality_gate_passed else 1


async def _async_main() -> int:
    """在任何私有读取和API调用前完成参数与历史状态校验。"""

    # 解析命令行不会读取项目文件。
    args = _parse_args()
    # 两把钥匙和首次结果状态在最前面验证。
    _validate_args(args)
    # 进入异步实验主体。
    return await _run(args)


if __name__ == "__main__":
    # 把质量门退出码交给PowerShell与PyCharm。
    raise SystemExit(asyncio.run(_async_main()))
