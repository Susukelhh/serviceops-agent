"""第36步：一次性验证Prompt v2在全新密封题集上的泛化能力。

默认模式只展示公开计划，不读取私有题集，也不读取API Key。只有用户同时确认密封集与付费API后，
才会调用真实千问；首次结果采用独占写入，任何后续复跑都必须明确标记为REGRESSION。
"""

# argparse提供密封集、付费、回归和私有诊断四个显式确认开关。
import argparse

# asyncio运行异步Embedding、检索和Grounded回答流程。
import asyncio

# json把强类型报告写成可阅读的UTF-8文本。
import json

# os提供刷盘、原子替换、独占硬链接和当前进程ID。
import os

# Iterator为文件锁上下文管理器标注yield类型。
from collections.abc import Iterator

# contextmanager保证首次运行锁在成功或异常时都能释放。
from contextlib import contextmanager

# sha256用于确认REGRESSION没有触碰首次冻结结果。
from hashlib import sha256

# Path统一声明配置、报告、锁和公开结果位置。
from pathlib import Path

# uuid4为临时文件生成不可碰撞的名称。
from uuid import uuid4

# PROJECT_ROOT保证PyCharm和PowerShell从不同目录启动时仍能定位项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings只在用户显式确认付费后读取.env中的千问配置。
from serviceops_agent.config.settings import Settings

# 第36步复用已经测试的事实级评分器、混合检索器和私有诊断writer。
from serviceops_agent.evaluation import (
    GroundedAnswerSuccessExperimentReport,
    GroundedAnswerSuccessSummary,
    PrivateGroundedAnswerDiagnosticCollector,
    load_grounded_answer_success_config,
    run_grounded_answer_success_experiment,
    write_private_grounded_answer_diagnostics,
)

# CONFIG_PATH只公开题目数量、SHA、模型与检索参数，不包含问题或金标正文。
CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v2_sealed_experiment.json"
)
# REPORT_PATH保存本机脱敏运行报告；runtime目录默认不会提交Git。
REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v2_sealed_report.json"
)
# FROZEN_RESULT_PATH只允许首次真实候选创建一次，后续绝不覆盖。
FROZEN_RESULT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/grounded_answer_v2_sealed_result.json"
)
# FIRST_RUN_LOCK_PATH防止两个终端同时发起首次付费盲测。
FIRST_RUN_LOCK_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v2_sealed_first_run.lock"
)


def _parse_args() -> argparse.Namespace:
    """解析第36步四个安全开关。"""

    # 默认命令只能看到公开计划，不能读取密封题目正文。
    parser = argparse.ArgumentParser(
        description="运行第36步Prompt v2全新密封盲测。"
    )
    # 第一把钥匙允许程序读取本机SHA冻结的私有题集。
    parser.add_argument(
        "--confirm-sealed",
        action="store_true",
        help="确认读取第36步本机密封题集。",
    )
    # 第二把钥匙允许创建真实千问客户端并产生费用。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认调用真实千问Embedding与聊天模型。",
    )
    # 首次结果存在后，只有此开关才能把同一题集作为已揭晓回归集复跑。
    parser.add_argument(
        "--regression",
        action="store_true",
        help="明确本轮是已揭晓REGRESSION，不是新的盲测。",
    )
    # 只有付费REGRESSION才能保存问题、证据和回答，首次盲测不写原始诊断。
    parser.add_argument(
        "--write-private-diagnostics",
        action="store_true",
        help="仅在付费REGRESSION中保存本机私有诊断。",
    )
    # 返回argparse标准对象。
    return parser.parse_args()


def _validate_args(
    args: argparse.Namespace,
    *,
    frozen_result_exists: bool | None = None,
) -> None:
    """在读取私有文件、.env或模型配置前验证开关关系。"""

    # 测试可以注入状态；真实运行直接检查首次公开结果是否存在。
    first_result_exists = (
        FROZEN_RESULT_PATH.is_file()
        if frozen_result_exists is None
        else frozen_result_exists
    )
    # 真实付费必须同时确认允许读取密封题集。
    if args.confirm_paid_api and not args.confirm_sealed:
        # 缺少第一把钥匙时在任何文件或网络访问前停止。
        raise ValueError("--confirm-paid-api必须同时提供--confirm-sealed")
    # REGRESSION只能描述一次真实复跑，不能用于离线对照。
    if args.regression and not args.confirm_paid_api:
        # 防止用户把普通离线执行误认为历史回归。
        raise ValueError("--regression必须同时提供--confirm-paid-api")
    # 尚无首次结果时不存在可以复跑的历史基点。
    if args.regression and not first_result_exists:
        # 首次运行必须先独占保存公开证据。
        raise ValueError("--regression要求首次第36步冻结结果已经存在")
    # 原始诊断包含私有题目，只允许已经揭晓后的付费回归生成。
    if args.write_private_diagnostics and not (
        args.confirm_sealed and args.confirm_paid_api and args.regression
    ):
        # 固定错误不打印任何私有路径。
        raise ValueError(
            "--write-private-diagnostics必须同时提供"
            "--confirm-sealed --confirm-paid-api --regression"
        )
    # 首次结果存在后，普通付费命令不得再次冒充一次性盲测。
    if args.confirm_paid_api and first_result_exists and not args.regression:
        # 明确要求用户承认题集已经揭晓。
        raise ValueError("第36步首次结果已存在；复跑请添加--regression")


def _snapshot(path: Path) -> tuple[str, int, int]:
    """记录首次结果内容SHA、字节数和修改时间。"""

    # 一次读取同时用于内容摘要和长度计算。
    content = path.read_bytes()
    # 纳秒修改时间可发现内容相同但文件被重写的情况。
    modified_ns = path.stat().st_mtime_ns
    # 三个值共同保护历史证据。
    return sha256(content).hexdigest(), len(content), modified_ns


def _assert_snapshot_unchanged(
    path: Path,
    expected: tuple[str, int, int],
) -> None:
    """确认回归没有删除、覆盖或重写首次冻结结果。"""

    # 历史文件缺失本身就是严重证据破坏。
    if not path.is_file():
        # 不做自动恢复，避免再次覆盖用户文件。
        raise RuntimeError("REGRESSION期间第36步首次结果被删除")
    # 内容、长度或修改时间变化都立即失败。
    if _snapshot(path) != expected:
        # 后续报告和诊断不再发布。
        raise RuntimeError("REGRESSION期间第36步首次结果发生变化")


def _write_atomic_replace(path: Path, payload: object) -> None:
    """把允许更新的runtime报告完整落盘后再原子替换。"""

    # 新克隆中runtime目录可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证os.replace不跨磁盘分区。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 先在内存完成序列化，失败不会留下半份JSON。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式避免极小概率UUID碰撞导致覆盖。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整JSON。
            handle.write(content)
            # 清空Python缓冲区。
            handle.flush()
            # 要求操作系统把数据完整落盘。
            os.fsync(handle.fileno())
        # 最终路径只会看到旧完整文件或新完整文件。
        os.replace(temporary_path, path)
    finally:
        # 任意异常都清理partial文件。
        temporary_path.unlink(missing_ok=True)


def _write_atomic_exclusive(path: Path, payload: object) -> None:
    """只创建一次首次冻结结果，目标已存在时绝不覆盖。"""

    # 公开结果目录在新克隆中可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时路径与最终路径保持同一文件系统。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 首次结果同样先在内存完成序列化。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式创建唯一临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 写入完整公开聚合结果。
            handle.write(content)
            # 刷新语言层缓冲区。
            handle.flush()
            # 在发布前完成物理刷盘。
            os.fsync(handle.fileno())
        # 硬链接在目标已存在时原子失败，因此没有覆盖窗口。
        os.link(temporary_path, path)
    finally:
        # 成功后删除临时别名，失败也不保留半文件。
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _first_paid_run_lock(*, enabled: bool) -> Iterator[None]:
    """阻止两个终端并发产生两份“首次”费用与结果。"""

    # 计划、离线和REGRESSION不争夺首次写入锁。
    if not enabled:
        # 直接进入调用方逻辑。
        yield
        # 上下文结束。
        return
    # runtime目录默认被Git忽略。
    FIRST_RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # 只有一个进程能以x模式创建锁文件。
        with FIRST_RUN_LOCK_PATH.open("x", encoding="utf-8", newline="\n") as lock:
            # PID只帮助排查异常退出，不包含题目或Key。
            lock.write(f"pid={os.getpid()}\n")
            # 锁标识也完整刷盘。
            lock.flush()
            os.fsync(lock.fileno())
    except FileExistsError as error:
        # 第二个进程必须在读取Key和调用API前停止。
        raise RuntimeError("已有第36步首次付费实验正在运行") from error
    try:
        # 锁覆盖整个API调用和首次结果发布阶段。
        yield
    finally:
        # 正常、Gate失败或异常都会释放本进程创建的锁。
        FIRST_RUN_LOCK_PATH.unlink(missing_ok=True)


def _print_summary(title: str, summary: GroundedAnswerSuccessSummary) -> None:
    """只输出唯一主指标、红线数量和脱敏失败ID。"""

    # 标题区分离线风险对照与真实千问候选。
    print(f"\n{title}：")
    # 分子与分母比单独百分比更诚实。
    print(
        "端到端有据回答成功率："
        f"{summary.passed_cases}/{summary.total_cases} = "
        f"{summary.grounded_answer_success_rate:.2%}"
    )
    # 红线是零容忍否决条件，不包装成第二个平均指标。
    print(f"红线失败题：{len(summary.red_line_case_ids)}条")
    # Gate同时考虑80%比例门和红线否决。
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 只显示失败的稳定ID与有限原因码。
    for result in summary.results:
        # 通过题无需制造输出噪声。
        if result.passed:
            # 继续下一题。
            continue
        # 不打印问题、答案、事实标签或证据正文。
        print(f"- {result.case_id}：{','.join(result.failure_codes)}")


def _public_result_payload(
    report: GroundedAnswerSuccessExperimentReport,
) -> dict[str, object]:
    """从强类型Report提取不含私有正文的首次公开证据。"""

    # 运行器返回的真实类型在付费路径上一定具有qwen_candidate。
    candidate = report.qwen_candidate
    # 防御式检查避免写出没有真实候选的伪冻结结果。
    if candidate is None:
        # 固定错误不会泄漏任何输入。
        raise RuntimeError("第36步真实候选结果缺失")
    # 只保存复现身份、唯一指标和有限失败码。
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
            {
                "case_id": result.case_id,
                "failure_codes": result.failure_codes,
            }
            for result in candidate.results
            if not result.passed
        ],
        "contains_private_questions": False,
        "contains_model_answers": False,
        "contains_fact_rule_text": False,
    }


async def _run(args: argparse.Namespace) -> int:
    """执行公开计划、离线密封对照或真实一次性候选。"""

    # REGRESSION开始前记录首次结果，结束后必须逐字节保持不变。
    frozen_snapshot = _snapshot(FROZEN_RESULT_PATH) if args.regression else None
    # 只有显式回归诊断开关才在内存收集私有原始数据。
    collector = (
        PrivateGroundedAnswerDiagnosticCollector()
        if args.write_private_diagnostics
        else None
    )
    # 公开配置不含任何问题或答案正文。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 只有真实付费路径才允许Settings读取.env。
    settings = (
        Settings()
        if args.confirm_paid_api
        else Settings.model_construct(telemetry_enabled=False)
    )
    # 复用第34步已验证的完整Qdrant+BM25+RRF+Grounded评分链路。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
        confirm_blind=args.confirm_sealed,
        confirm_paid_api=args.confirm_paid_api,
        confirm_regression=args.regression,
        private_diagnostic_collector=collector,
    )
    # 回归必须先证明首次证据未变，再发布本轮runtime报告。
    if frozen_snapshot is not None:
        # frozen_snapshot在args.regression为True时必定存在。
        _assert_snapshot_unchanged(FROZEN_RESULT_PATH, frozen_snapshot)
    # runtime报告可更新，但始终原子写入且不含私有正文。
    _write_atomic_replace(REPORT_PATH, report.model_dump(mode="json"))

    # 先展示公开冻结身份和费用上限计划。
    print("=== ServiceOps 第36步：Prompt v2全新密封盲测 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"评分规则：{report.evaluator_version}")
    print(
        f"密封规模：{report.blind_case_count}题；"
        f"题集SHA：{report.blind_dataset_sha256}"
    )
    print(f"候选指纹：{report.candidate_fingerprint}")
    print(
        f"真实候选计划：Embedding约{report.planned_embedding_requests}次，"
        f"聊天最多{report.planned_chat_calls}次"
    )
    # 默认模式没有读取私有题集。
    if report.offline_baseline is None:
        # 明确费用为零。
        print("\n密封题集：未读取；千问：未调用（费用0元）")
        print("下一步离线校验参数：--confirm-sealed")
        print(f"报告：{REPORT_PATH}")
        # 展示计划属于正常成功。
        return 0
    # 密封题集确认后先展示完全离线风险对照。
    _print_summary("Hash+BM25+RRF+Extractive离线对照", report.offline_baseline)
    # 未确认付费时不创建真实候选。
    if report.qwen_candidate is None:
        # 离线结果不能冒充Prompt v2结论。
        print("\nPrompt v2真实候选：未调用（费用0元）")
        print("一次性付费盲测需同时添加--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线基线Gate失败是风险对照，不让PyCharm显示脚本异常。
        return 0
    # 展示真实候选的唯一主指标。
    _print_summary("千问Prompt v2冻结候选", report.qwen_candidate)
    # 打印调用量，不估算可能变化的金额。
    print(
        f"实际Embedding请求：{report.actual_embedding_requests}次；"
        f"输入Token：{report.actual_embedding_input_tokens}；"
        f"实际聊天调用：{report.actual_chat_calls}次"
    )
    # 首次运行独占创建公开结果；REGRESSION永远不覆盖。
    if not args.regression:
        # 只写脱敏聚合，不保存原问题或回答。
        _write_atomic_exclusive(
            FROZEN_RESULT_PATH,
            _public_result_payload(report),
        )
        # 告知公开证据位置。
        print(f"首次冻结结果：{FROZEN_RESULT_PATH}")
    else:
        # 明确本轮不能再称新盲测。
        print("本轮标记为REGRESSION，不覆盖第36步首次结果。")
        # 只有第四把钥匙才保存本机私有诊断。
        if collector is not None:
            # writer目录已被Git和Docker忽略。
            private_path = write_private_grounded_answer_diagnostics(
                collector,
                experiment_id=report.experiment_id,
                experiment_version=report.experiment_version,
                dataset_sha256=report.blind_dataset_sha256,
                candidate_fingerprint=report.candidate_fingerprint,
                profile_id=report.qwen_candidate.profile_id,
            )
            # 控制台只打印路径，不输出原始内容。
            print(f"本机私有诊断：{private_path}")
    # 最后展示脱敏runtime报告位置。
    print(f"报告：{REPORT_PATH}")
    # 真实候选Gate决定进程退出码。
    return 0 if report.qwen_candidate.quality_gate_passed else 1


async def _async_main() -> int:
    """在安全状态机与首次锁保护下运行第36步。"""

    # 参数解析不会读取项目文件。
    args = _parse_args()
    # 参数关系在配置、私有题集和.env之前验证。
    _validate_args(args)
    # 只有首次真实付费运行需要互斥锁。
    with _first_paid_run_lock(
        enabled=args.confirm_paid_api and not args.regression
    ):
        # 锁内覆盖模型调用和首次结果发布全过程。
        return await _run(args)


if __name__ == "__main__":
    # 把Gate退出码交给PowerShell和PyCharm。
    raise SystemExit(asyncio.run(_async_main()))
