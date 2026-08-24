"""第38步：用全新密封题集验收范围门v2与最终状态评分。

默认运行只展示公开计划；加入 ``--confirm-sealed`` 后只运行离线对照；只有再加入
``--confirm-paid-api`` 才会调用真实千问。首次真实结果采用独占写入；揭晓后的复跑必须显式标记为
REGRESSION，且绝不能覆盖首次冻结结果。
"""

# argparse负责解析两把需要用户亲自打开的安全开关。
import argparse

# asyncio负责启动异步Embedding、检索和回答链路。
import asyncio

# json把脱敏报告保存成便于检查的UTF-8文件。
import json

# os提供刷盘、原子替换、独占硬链接和进程编号。
import os

# Iterator用于标注首次付费锁上下文管理器的yield类型。
from collections.abc import Iterator

# contextmanager保证成功、Gate失败或异常时都会释放运行锁。
from contextlib import contextmanager

# sha256用于确认REGRESSION前后首次冻结文件逐字节不变。
from hashlib import sha256

# Path统一管理配置、报告、锁和冻结结果位置。
from pathlib import Path

# uuid4给临时文件生成不会碰撞的名称。
from uuid import uuid4

# PROJECT_ROOT让PyCharm和PowerShell从任意工作目录启动都能找到项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings只在用户确认付费后读取.env中的千问配置。
from serviceops_agent.config.settings import Settings

# 第38步复用经过测试的事实评分器、混合检索器和强类型报告。
from serviceops_agent.evaluation import (
    GroundedAnswerSuccessExperimentReport,
    GroundedAnswerSuccessSummary,
    PrivateGroundedAnswerDiagnosticCollector,
    load_grounded_answer_success_config,
    run_grounded_answer_success_experiment,
    write_private_grounded_answer_diagnostics,
)

# CONFIG_PATH只保存题量、SHA和候选参数，不含密封问题与金标正文。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v3_sealed_experiment.json"
)
# REPORT_PATH保存本机脱敏运行报告；runtime目录默认不进入Git。
REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v3_sealed_report.json"
)
# FROZEN_RESULT_PATH只允许第一次真实实验创建一次。
FROZEN_RESULT_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/results/grounded_answer_v3_sealed_result.json"
)
# FIRST_RUN_LOCK_PATH防止两个终端同时发起“首次”付费实验。
FIRST_RUN_LOCK_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v3_sealed_first_run.lock"
)


def _parse_args() -> argparse.Namespace:
    """解析密封集、付费、回归和私有诊断四个安全开关。"""

    # 不带参数时程序只能展示公开实验计划，费用为零。
    parser = argparse.ArgumentParser(description="运行第38步Prompt v2全新密封盲测。")
    # 第一把钥匙允许读取已经SHA冻结的本机私有题集。
    parser.add_argument(
        "--confirm-sealed",
        action="store_true",
        help="确认读取第38步本机密封题集。",
    )
    # 第二把钥匙允许创建千问客户端并产生真实费用。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认调用真实千问Embedding和聊天模型。",
    )
    # 首次结果存在后，只有这个开关才能把题集作为已揭晓回归集复跑。
    parser.add_argument(
        "--regression",
        action="store_true",
        help="明确本轮是已揭晓REGRESSION，不是新的盲测。",
    )
    # 原始问题、证据和回答只允许保存在本机被Git忽略的私有诊断目录。
    parser.add_argument(
        "--write-private-diagnostics",
        action="store_true",
        help="仅在付费REGRESSION中保存本机私有诊断。",
    )
    # 返回标准参数对象。
    return parser.parse_args()


def _validate_args(
    args: argparse.Namespace,
    *,
    frozen_result_exists: bool | None = None,
) -> None:
    """在读取私有数据、.env或模型配置前验证安全状态。"""

    # 测试可注入文件状态；真实运行则检查冻结结果是否已经存在。
    result_exists = (
        FROZEN_RESULT_PATH.is_file()
        if frozen_result_exists is None
        else frozen_result_exists
    )
    # 付费调用必须同时得到读取密封集的明确许可。
    if args.confirm_paid_api and not args.confirm_sealed:
        # 在网络与私有文件访问前快速失败。
        raise ValueError("--confirm-paid-api必须同时提供--confirm-sealed")
    # REGRESSION是付费真实复跑，不能用于默认计划或离线基线。
    if args.regression and not args.confirm_paid_api:
        # 防止普通离线执行被误写成历史回归。
        raise ValueError("--regression必须同时提供--confirm-paid-api")
    # 尚无首次结果时不存在可供诊断的已揭晓基点。
    if args.regression and not result_exists:
        # 首次运行必须先独占保存冻结结果。
        raise ValueError("--regression要求第38步首次冻结结果已经存在")
    # 私有诊断必须同时具备读取、付费和已揭晓三重确认。
    if args.write_private_diagnostics and not (
        args.confirm_sealed and args.confirm_paid_api and args.regression
    ):
        # 在读取私有文件和.env前快速失败。
        raise ValueError(
            "--write-private-diagnostics必须同时提供"
            "--confirm-sealed --confirm-paid-api --regression"
        )
    # 首次结果存在后，普通付费命令不能再次冒充新盲测。
    if args.confirm_paid_api and result_exists and not args.regression:
        # 明确要求调用者承认题集已揭晓。
        raise ValueError("第38步首次结果已存在；诊断复跑请添加--regression")


def _snapshot(path: Path) -> tuple[str, int, int]:
    """记录首次结果的内容SHA、字节数和纳秒修改时间。"""

    # 一次读取同时用于摘要与长度计算。
    content = path.read_bytes()
    # 修改时间可以发现内容相同但文件被重写的情况。
    modified_ns = path.stat().st_mtime_ns
    # 三个值共同构成历史保护快照。
    return sha256(content).hexdigest(), len(content), modified_ns


def _assert_snapshot_unchanged(
    path: Path,
    expected: tuple[str, int, int],
) -> None:
    """确认REGRESSION没有删除、覆盖或重写首次冻结结果。"""

    # 历史文件缺失本身就是证据破坏。
    if not path.is_file():
        # 不自动恢复，避免再次覆盖用户文件。
        raise RuntimeError("REGRESSION期间第38步首次结果被删除")
    # 内容、长度或修改时间任一变化都立即失败。
    if _snapshot(path) != expected:
        # 后续报告和诊断不再发布。
        raise RuntimeError("REGRESSION期间第38步首次结果发生变化")


def _write_atomic_replace(path: Path, payload: object) -> None:
    """完整写好runtime报告后再原子替换旧版本。"""

    # 新克隆的runtime目录可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证替换操作不跨磁盘。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 先完成内存序列化，避免编码失败留下半份JSON。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式防止极小概率的临时文件名碰撞。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整内容。
            handle.write(content)
            # 清空Python缓冲区。
            handle.flush()
            # 要求操作系统完成刷盘。
            os.fsync(handle.fileno())
        # 目标只会看到旧完整文件或新完整文件。
        os.replace(temporary_path, path)
    finally:
        # 任意异常都不保留partial文件。
        temporary_path.unlink(missing_ok=True)


def _write_atomic_exclusive(path: Path, payload: object) -> None:
    """独占创建首次冻结结果，目标已存在时绝不覆盖。"""

    # 公开结果目录在新克隆中可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时路径和最终路径位于同一文件系统。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 首次结果先在内存完成序列化。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式独占创建临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整脱敏结果。
            handle.write(content)
            # 刷新语言层缓冲区。
            handle.flush()
            # 发布前完成操作系统刷盘。
            os.fsync(handle.fileno())
        # 硬链接在目标已存在时会原子失败，因此没有覆盖窗口。
        os.link(temporary_path, path)
    finally:
        # 删除临时别名，成功与失败都不会留下半文件。
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _first_paid_run_lock(*, enabled: bool) -> Iterator[None]:
    """防止两个终端并发产生两份首次费用和结果。"""

    # 默认计划和离线对照不需要占用付费锁。
    if not enabled:
        # 直接执行调用方逻辑。
        yield
        # 上下文到此结束。
        return
    # runtime目录由Git忽略，可以安全放置短期锁文件。
    FIRST_RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 只有一个进程能以x模式创建锁。
        with FIRST_RUN_LOCK_PATH.open("x", encoding="utf-8", newline="\n") as lock:
            # PID只帮助排查异常退出，不含题目和Key。
            lock.write(f"pid={os.getpid()}\n")
            # 锁标记同样完整刷盘。
            lock.flush()
            os.fsync(lock.fileno())
    except FileExistsError as error:
        # 第二个进程必须在读取Key和调用API前停止。
        raise RuntimeError("已有第38步首次付费实验正在运行") from error
    try:
        # 锁覆盖模型调用和首次结果发布全过程。
        yield
    finally:
        # 正常结束、Gate失败或异常都会释放本进程创建的锁。
        FIRST_RUN_LOCK_PATH.unlink(missing_ok=True)


def _print_summary(title: str, summary: GroundedAnswerSuccessSummary) -> None:
    """只输出唯一主指标、红线数量和脱敏失败ID。"""

    # 标题区分离线风险对照与千问冻结候选。
    print(f"\n{title}：")
    # 同时展示分子、分母和百分比，避免只看百分比产生错觉。
    print(
        "端到端有据回答成功率："
        f"{summary.passed_cases}/{summary.total_cases} = "
        f"{summary.grounded_answer_success_rate:.2%}"
    )
    # 红线属于零容忍否决条件，不包装成第二个平均分。
    print(f"红线失败题：{len(summary.red_line_case_ids)}条")
    # Gate同时要求成功率达到80%并且没有红线错误。
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 只输出稳定ID和有限原因码，不输出问题、回答或事实规则。
    for result in summary.results:
        # 通过题不需要逐条打印。
        if result.passed:
            # 继续处理下一题。
            continue
        # 失败信息足够定位本机私有数据，但不会泄漏正文。
        print(f"- {result.case_id}：{','.join(result.failure_codes)}")


def _public_result_payload(
    report: GroundedAnswerSuccessExperimentReport,
) -> dict[str, object]:
    """提取可进入Git、但不含私有正文的首次实验凭证。"""

    # 只有真实付费路径才应生成qwen_candidate。
    candidate = report.qwen_candidate
    # 防止错误地把离线结果保存成真实冻结结论。
    if candidate is None:
        # 固定异常消息不泄漏任何输入。
        raise RuntimeError("第38步真实候选结果缺失")
    # 只保存复现身份、唯一主指标和有限失败码。
    return {
        "experiment_id": report.experiment_id,
        "experiment_version": report.experiment_version,
        "run_kind": "FIRST_SEALED_EVALUATION",
        "dataset_sha256": report.blind_dataset_sha256,
        "candidate_fingerprint": report.candidate_fingerprint,
        "profile_id": candidate.profile_id,
        "total_cases": candidate.total_cases,
        "passed_cases": candidate.passed_cases,
        "grounded_answer_success_rate": candidate.grounded_answer_success_rate,
        "red_line_case_ids": candidate.red_line_case_ids,
        "quality_gate_passed": candidate.quality_gate_passed,
        "quality_gate_failures": candidate.quality_gate_failures,
        "failed_cases": [
            {"case_id": result.case_id, "failure_codes": result.failure_codes}
            for result in candidate.results
            if not result.passed
        ],
        "contains_private_questions": False,
        "contains_model_answers": False,
        "contains_fact_rule_text": False,
    }


async def _run(args: argparse.Namespace) -> int:
    """执行公开计划、离线密封对照或一次性真实候选。"""

    # REGRESSION开始前先记录首次结果，结束后必须逐字节保持不变。
    frozen_snapshot = _snapshot(FROZEN_RESULT_PATH) if args.regression else None
    # 只有显式诊断开关才在内存收集原问题、证据和模型原始答案。
    collector = (
        PrivateGroundedAnswerDiagnosticCollector()
        if args.write_private_diagnostics
        else None
    )
    # 公开配置只含SHA、计数和候选参数。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 未确认付费时构造无Key设置，保证默认和离线路径不会读取.env。
    settings = (
        Settings()
        if args.confirm_paid_api
        else Settings.model_construct(telemetry_enabled=False)
    )
    # 运行完整范围门、Qdrant语义召回、BM25、RRF、Grounded回答和事实评分链路。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
        confirm_blind=args.confirm_sealed,
        confirm_paid_api=args.confirm_paid_api,
        confirm_regression=args.regression,
        private_diagnostic_collector=collector,
    )
    # 先证明历史文件没有变化，再发布本轮脱敏runtime报告。
    if frozen_snapshot is not None:
        # frozen_snapshot在REGRESSION路径上一定存在。
        _assert_snapshot_unchanged(FROZEN_RESULT_PATH, frozen_snapshot)
    # runtime报告可更新，但必须保持完整原子写入。
    _write_atomic_replace(REPORT_PATH, report.model_dump(mode="json"))
    # 先展示揭晓前已经冻结的公开身份。
    print("=== ServiceOps 第38步：范围门v2全新密封盲测 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"评分规则：{report.evaluator_version}")
    print(f"密封规模：{report.blind_case_count}题；题集SHA：{report.blind_dataset_sha256}")
    print(f"候选指纹：{report.candidate_fingerprint}")
    print(
        f"真实候选计划：Embedding约{report.planned_embedding_requests}次，"
        f"聊天最多{report.planned_chat_calls}次"
    )
    # 默认模式没有读取私有题集。
    if report.offline_baseline is None:
        # 明确告诉用户本轮费用为零。
        print("\n密封题集：未读取；千问：未调用（费用0元）")
        print("下一步离线校验参数：--confirm-sealed")
        print(f"报告：{REPORT_PATH}")
        # 展示计划属于正常成功。
        return 0
    # 读取密封题集后先展示完全离线的风险对照。
    _print_summary("Hash+BM25+RRF+Extractive离线对照", report.offline_baseline)
    # 没有第二把钥匙时不会创建真实候选。
    if report.qwen_candidate is None:
        # 离线对照不能冒充范围门v2的真实泛化结论。
        print("\n范围门v2真实候选：未调用（费用0元）")
        print("一次性付费盲测需同时添加--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线Gate失败只是对照，不让PyCharm显示脚本异常。
        return 0
    # 展示真实候选唯一主指标。
    _print_summary("千问范围门v2冻结候选", report.qwen_candidate)
    # 调用量比金额估算更可复核。
    print(
        f"实际Embedding请求：{report.actual_embedding_requests}次；"
        f"输入Token：{report.actual_embedding_input_tokens}；"
        f"实际聊天调用：{report.actual_chat_calls}次"
    )
    # 首次运行独占保存公开结果；REGRESSION永远不覆盖。
    if not args.regression:
        # 任何已有目标都会使独占写入失败。
        _write_atomic_exclusive(FROZEN_RESULT_PATH, _public_result_payload(report))
        # 展示公开冻结凭证位置。
        print(f"首次冻结结果：{FROZEN_RESULT_PATH}")
    else:
        # 明确本轮不再是新的密封盲测。
        print("本轮标记为REGRESSION，不覆盖第38步首次结果。")
        # 只有第四把钥匙才保存本机原始诊断。
        if collector is not None:
            # writer固定写入被Git和Docker忽略的私有目录。
            private_path = write_private_grounded_answer_diagnostics(
                collector,
                experiment_id=report.experiment_id,
                experiment_version=report.experiment_version,
                dataset_sha256=report.blind_dataset_sha256,
                candidate_fingerprint=report.candidate_fingerprint,
                profile_id=report.qwen_candidate.profile_id,
            )
            # 控制台只输出路径，不打印问题、答案或证据正文。
            print(f"本机私有诊断：{private_path}")
    # 展示本机脱敏报告位置。
    print(f"报告：{REPORT_PATH}")
    # 真实Gate决定进程退出码；FAIL为1是实验结论，不代表程序崩溃。
    return 0 if report.qwen_candidate.quality_gate_passed else 1


async def _async_main() -> int:
    """先完成安全校验，再在首次锁保护下运行。"""

    # 解析命令行不会读取项目文件。
    args = _parse_args()
    # 参数和历史状态在私有文件、.env与API访问前验证。
    _validate_args(args)
    # 只有首次真实付费路径需要互斥锁；REGRESSION保护的是已有快照。
    with _first_paid_run_lock(
        enabled=args.confirm_paid_api and not args.regression
    ):
        # 锁内覆盖完整模型调用和首次结果发布。
        return await _run(args)


if __name__ == "__main__":
    # 把质量门退出码交给PowerShell与PyCharm。
    raise SystemExit(asyncio.run(_async_main()))
