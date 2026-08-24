"""第34步：运行事实级端到端有据回答成功率盲测。"""

# argparse提供私有盲测、真实付费候选和已揭晓回归三道显式开关。
import argparse

# asyncio负责运行异步Grounded回答器。
import asyncio

# json把脱敏强类型报告保存为UTF-8文件。
import json

# os提供跨进程独占锁标识、完整落盘和同目录原子发布能力。
import os

# Iterator标注同步上下文管理器的yield类型。
from collections.abc import Iterator

# contextmanager把“创建锁—执行—清理锁”封装为不易漏释放的边界。
from contextlib import contextmanager

# sha256用于证明REGRESSION前后首次公开结果字节完全没有变化。
from hashlib import sha256

# Path声明稳定配置、运行报告和公开冻结结果路径。
from pathlib import Path

# uuid4让每个临时文件具有不可碰撞名称，避免并发进程互相覆盖。
from uuid import uuid4

# PROJECT_ROOT保证PyCharm从任意Working directory启动都能定位项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings读取.env，但默认和离线盲测路径都不会使用API Key。
from serviceops_agent.config.settings import Settings

# 第34步公共类型与运行器是本脚本的唯一业务依赖。
from serviceops_agent.evaluation import (
    GroundedAnswerSuccessSummary,
    PrivateGroundedAnswerDiagnosticCollector,
    load_grounded_answer_success_config,
    run_grounded_answer_success_experiment,
    write_private_grounded_answer_diagnostics,
)

# CONFIG_PATH只保存盲测数量、SHA、候选指纹和质量门，不含题目正文。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/grounded_answer_success_experiment.json"
# REPORT_PATH保存本机完整脱敏逐题结果，runtime目录不会提交Git。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/grounded_answer_success_report.json"
# FROZEN_RESULT_PATH只在首次真实千问盲测后保存可公开聚合证据。
FROZEN_RESULT_PATH: Path = (
    PROJECT_ROOT
    / "data/evaluation/results/grounded_answer_success_v1_frozen_result.json"
)
# FIRST_RUN_LOCK_PATH只保护首次付费运行，runtime目录不会进入Git或Docker镜像。
FIRST_RUN_LOCK_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_success_first_run.lock"
)


def _parse_args() -> argparse.Namespace:
    """解析盲测、付费、回归和私有诊断四道明确开关。"""

    # 默认执行只展示计划，不读取私有盲测正文。
    parser = argparse.ArgumentParser(
        description="运行第34步端到端有据回答成功率盲测。"
    )
    # 第一把钥匙允许读取本机sealed题集并运行零费用对照。
    parser.add_argument(
        "--confirm-blind",
        action="store_true",
        help="确认读取SHA冻结的本机私有盲测集。",
    )
    # 第二把钥匙允许真实千问Embedding和qwen-plus结构化回答。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认产生真实千问Embedding与聊天调用费用。",
    )
    # 已有首次冻结结果后，只有显式回归模式才能再次付费运行同一题集。
    parser.add_argument(
        "--regression",
        action="store_true",
        help="将已揭晓题集作为回归集复跑，不再宣称首次盲测。",
    )
    # 第四把钥匙只允许REGRESSION把原问题、答案和证据写入Git忽略的本机目录。
    parser.add_argument(
        "--write-private-diagnostics",
        action="store_true",
        help="仅在付费REGRESSION中写入本机私有原始诊断，不进入公开报告。",
    )
    # 返回命令行参数对象。
    return parser.parse_args()


def _validate_argument_contract(
    args: argparse.Namespace,
    *,
    frozen_result_exists: bool | None = None,
) -> None:
    """在读取配置、私有题目或API Key前校验四道安全开关。"""

    # 测试可以注入是否存在；真实脚本使用文件系统中的首次冻结证据。
    first_result_exists = (
        FROZEN_RESULT_PATH.is_file()
        if frozen_result_exists is None
        else frozen_result_exists
    )
    # 付费候选必须同时确认私有盲测读取。
    if args.confirm_paid_api and not args.confirm_blind:
        # 在任何文件或网络访问前快速失败。
        raise ValueError("--confirm-paid-api必须同时提供--confirm-blind")
    # regression只对付费复跑有意义，避免用户误以为普通离线运行改变历史。
    if args.regression and not args.confirm_paid_api:
        # 给出明确参数关系。
        raise ValueError("--regression必须同时提供--confirm-paid-api")
    # 没有首次冻结证据就不能把运行伪装成回归。
    if args.regression and not first_result_exists:
        # 先完成并保存首次盲测，才存在REGRESSION的历史基点。
        raise ValueError("--regression要求首次真实盲测冻结结果已经存在")
    # 私有原始诊断必须同时经过盲测、付费和回归三重确认。
    if args.write_private_diagnostics and not (
        args.confirm_blind and args.confirm_paid_api and args.regression
    ):
        # 缺任何一把钥匙都禁止创建包含原问题或答案的文件。
        raise ValueError(
            "--write-private-diagnostics必须同时提供"
            "--confirm-blind --confirm-paid-api --regression"
        )
    # 已存在首次结果时，普通付费运行不得再次冒充首次盲测。
    if args.confirm_paid_api and first_result_exists and not args.regression:
        # 用户必须显式承认这是回归复跑。
        raise ValueError("首次真实盲测结果已存在；复跑请添加--regression")


def _snapshot_frozen_result(path: Path) -> tuple[str, int, int]:
    """记录首次结果的内容摘要、字节数和纳秒修改时间。"""

    # read_bytes不会解析JSON，也不会改变原文件。
    content = path.read_bytes()
    # stat在读取后记录长度和修改时间，用于发现意外覆盖或触碰。
    stat_result = path.stat()
    # 三个值共同构成回归运行前快照。
    return sha256(content).hexdigest(), len(content), stat_result.st_mtime_ns


def _assert_frozen_result_unchanged(
    path: Path,
    expected_snapshot: tuple[str, int, int],
) -> None:
    """确认回归运行没有覆盖、截断或重写首次公开结果。"""

    # 缺失文件本身就是严重历史证据破坏。
    if not path.is_file():
        # 固定错误不包含文件正文。
        raise RuntimeError("REGRESSION运行期间首次冻结结果被删除")
    # 内容或元数据任一变化都停止后续私有诊断写入。
    if _snapshot_frozen_result(path) != expected_snapshot:
        # 不尝试自动恢复，以免再次覆盖用户文件。
        raise RuntimeError("REGRESSION运行期间首次冻结结果发生变化")


def _write_text_atomic_replace(path: Path, content: str) -> None:
    """先完整写入同目录临时文件，再原子替换允许更新的运行报告。"""

    # 目标目录可能在全新克隆中尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证原子发布不跨磁盘分区。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    try:
        # x模式防止极小概率UUID碰撞时覆盖其他进程的临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as temp_file:
            # 一次写入已经完成序列化的UTF-8文本。
            temp_file.write(content)
            # 把Python缓冲区推送给操作系统。
            temp_file.flush()
            # 要求操作系统把文件内容完整落盘后再发布路径。
            os.fsync(temp_file.fileno())
        # runtime报告允许被新一轮完整报告替换，但不会暴露半份JSON。
        os.replace(temporary_path, path)
    finally:
        # 序列化、写入或发布异常时删除残留partial文件。
        temporary_path.unlink(missing_ok=True)


def _write_text_atomic_exclusive(path: Path, content: str) -> None:
    """完整落盘后用硬链接只创建一次不可覆盖的首次冻结结果。"""

    # 目标目录在首次运行前可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件和最终结果位于同一文件系统，硬链接才能原子发布。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    try:
        # 先把完整JSON写到外部不可见的唯一临时路径。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as temp_file:
            # 写入完整内容。
            temp_file.write(content)
            # 清空语言层缓冲区。
            temp_file.flush()
            # 在创建公开名称前确保字节已落盘。
            os.fsync(temp_file.fileno())
        # os.link在目标已存在时原子失败，绝不会覆盖首次证据。
        os.link(temporary_path, path)
    finally:
        # 成功后删除临时别名；失败也不留下半文件。
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _exclusive_first_paid_run_lock(*, enabled: bool) -> Iterator[None]:
    """用只创建一次的锁文件阻止两个首次进程同时产生费用。"""

    # 离线、计划和REGRESSION不争夺首次写入权。
    if not enabled:
        # 直接进入调用方代码。
        yield
        # 结束上下文。
        return
    # runtime目录默认被Git忽略，可以安全保存短生命周期锁。
    FIRST_RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # x模式是跨进程原子操作；只有一个进程能创建成功。
        with FIRST_RUN_LOCK_PATH.open("x", encoding="utf-8", newline="\n") as lock_file:
            # PID只帮助排查卡住的本机进程，不包含Key或盲测内容。
            lock_file.write(f"pid={os.getpid()}\n")
            # 锁标识也必须先完整落盘。
            lock_file.flush()
            os.fsync(lock_file.fileno())
    except FileExistsError as error:
        # 第二个进程在读取Key或创建模型客户端前停止，费用保持为零。
        raise RuntimeError(
            "已有首次付费实验正在运行；若确认进程已异常退出，再人工删除runtime锁文件"
        ) from error
    try:
        # 锁文件保持存在，覆盖整个付费调用和首次结果发布阶段。
        yield
    finally:
        # 正常、Gate失败或异常退出都会释放本进程创建的锁。
        FIRST_RUN_LOCK_PATH.unlink(missing_ok=True)


def _print_summary(title: str, summary: GroundedAnswerSuccessSummary) -> None:
    """只突出一个质量数字，并打印有限Bad Case原因。"""

    # 标题区分离线风险对照与真实冻结候选。
    print(f"\n{title}：")
    # 通过数和总数比单独百分比更诚实。
    print(
        "端到端有据回答成功率："
        f"{summary.passed_cases}/{summary.total_cases} = "
        f"{summary.grounded_answer_success_rate:.2%}"
    )
    # 红线只作为否决条件，不包装成第二个平均分。
    print(f"红线失败题：{len(summary.red_line_case_ids)}条")
    # Gate综合单一比例门和严重错误否决。
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 只打印未通过的稳定ID与有限原因码，不打印私有问题、答案和金标。
    failed_results = [result for result in summary.results if not result.passed]
    # 明确输出失败数量。
    print(f"失败样本：{len(failed_results)}条")
    # 逐题展示定位信息。
    for result in failed_results:
        # 逗号连接稳定失败码便于复制搜索。
        failure_text = ",".join(result.failure_codes)
        # 不输出matched facts，减少盲测标签泄漏。
        print(f"- {result.case_id}：{failure_text}")


def _write_public_frozen_result(
    summary: GroundedAnswerSuccessSummary,
    *,
    experiment_id: str,
    experiment_version: str,
    dataset_sha256: str,
    candidate_fingerprint: str,
    target_path: Path | None = None,
) -> None:
    """保存不含问题、答案和事实规则的首次真实盲测聚合证据。"""

    # 公开结果只保留复现身份、唯一指标和有限失败原因。
    payload = {
        "experiment_id": experiment_id,
        "experiment_version": experiment_version,
        "dataset_sha256": dataset_sha256,
        "candidate_fingerprint": candidate_fingerprint,
        "profile_id": summary.profile_id,
        "total_cases": summary.total_cases,
        "passed_cases": summary.passed_cases,
        "grounded_answer_success_rate": summary.grounded_answer_success_rate,
        "red_line_case_ids": summary.red_line_case_ids,
        "quality_gate_passed": summary.quality_gate_passed,
        "quality_gate_failures": summary.quality_gate_failures,
        "failed_cases": [
            {
                "case_id": result.case_id,
                "failure_codes": result.failure_codes,
            }
            for result in summary.results
            if not result.passed
        ],
        "contains_private_questions": False,
        "contains_model_answers": False,
        "contains_fact_labels": False,
    }
    # 真实脚本使用固定路径；测试可以注入临时文件验证独占写入。
    selected_path = target_path or FROZEN_RESULT_PATH
    # 先完整落盘再独占发布，既防半份JSON也防任何已有首次结果被覆盖。
    _write_text_atomic_exclusive(
        selected_path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


async def _run_validated(args: argparse.Namespace) -> int:
    """在参数和首次运行锁已经通过后执行对应实验模式。"""

    # 回归开始前保存首次证据快照，结束后必须逐字节一致。
    frozen_result_snapshot = (
        _snapshot_frozen_result(FROZEN_RESULT_PATH) if args.regression else None
    )
    # 只有第四把钥匙存在时才在内存创建私有collector。
    private_diagnostic_collector = (
        PrivateGroundedAnswerDiagnosticCollector()
        if args.write_private_diagnostics
        else None
    )
    # 加载不含私有正文的公开配置。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # 只有真实付费路径才读取.env和系统环境变量。
    settings = (
        Settings()
        if args.confirm_paid_api
        # model_construct只装入类默认值，不触碰.env或SERVICEOPS_*环境变量；
        # 离线路径也不使用它的模型字段。
        else Settings.model_construct(telemetry_enabled=False)
    )
    # 执行对应确认级别的实验。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
        confirm_blind=args.confirm_blind,
        confirm_paid_api=args.confirm_paid_api,
        confirm_regression=args.regression,
        private_diagnostic_collector=private_diagnostic_collector,
    )
    # 真实候选返回后先保护首次证据，再保存任何新报告。
    if frozen_result_snapshot is not None:
        # 回归绝不能以任何方式改写首次40%结果。
        _assert_frozen_result_unchanged(
            FROZEN_RESULT_PATH,
            frozen_result_snapshot,
        )
    # Pydantic JSON模式确保所有字段可序列化；原子替换不会留下半份JSON。
    _write_text_atomic_replace(
        REPORT_PATH,
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
    )

    # 先展示冻结身份和最坏费用计划。
    print("=== ServiceOps 第34步：端到端有据回答成功率 ===")
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    print(f"评分规则：{report.evaluator_version}")
    print(f"盲测规模：{report.blind_case_count}题；题集SHA：{report.blind_dataset_sha256}")
    print(f"候选指纹：{report.candidate_fingerprint}")
    print(
        f"真实候选计划：Embedding约{report.planned_embedding_requests}次，"
        f"聊天最多{report.planned_chat_calls}次"
    )

    # 无确认路径到此为止，证明没有读取题目正文。
    if report.offline_baseline is None:
        # 给用户下一条零费用命令。
        print("\n私有盲测：未读取；千问：未调用（费用0元）")
        print("先运行零费用揭晓：--confirm-blind")
        print(f"报告：{REPORT_PATH}")
        # 计划展示不是失败。
        return 0
    # 展示离线风险基线的唯一指标。
    _print_summary("Hash+BM25+RRF+Extractive离线对照", report.offline_baseline)

    # 没有真实候选时明确说明，不把基线结论冒充千问结果。
    if report.qwen_candidate is None:
        # 本轮完全零费用。
        print("\n真实千问候选：未调用（费用0元）")
        print("候选参数已经冻结；付费盲测需同时添加--confirm-paid-api")
        print(f"报告：{REPORT_PATH}")
        # 离线基线失败是预期诊断，不让PyCharm显示为脚本异常。
        return 0
    # 展示真实候选唯一成功率。
    _print_summary("千问冻结候选", report.qwen_candidate)
    # 打印实际费用相关调用量，不估算金额。
    print(
        f"实际Embedding请求：{report.actual_embedding_requests}次；"
        f"输入Token：{report.actual_embedding_input_tokens}；"
        f"实际聊天调用：{report.actual_chat_calls}次"
    )
    # 首次模式写入公开脱敏证据；回归模式不覆盖历史首次结果。
    if not args.regression:
        # 保存候选质量结论。
        _write_public_frozen_result(
            report.qwen_candidate,
            experiment_id=report.experiment_id,
            experiment_version=report.experiment_version,
            dataset_sha256=report.blind_dataset_sha256,
            candidate_fingerprint=report.candidate_fingerprint,
        )
        # 告知用户公开证据位置。
        print(f"首次冻结结果：{FROZEN_RESULT_PATH}")
    else:
        # 明确同一题集已经揭晓。
        print("本轮标记为REGRESSION，不覆盖首次盲测结果。")
        # 只有显式第四把钥匙才把原始输入输出写进私有忽略目录。
        if private_diagnostic_collector is not None:
            # writer不会接收Settings或Key，只序列化collector中的题、证据和模型草稿。
            private_diagnostic_path = write_private_grounded_answer_diagnostics(
                private_diagnostic_collector,
                experiment_id=report.experiment_id,
                experiment_version=report.experiment_version,
                dataset_sha256=report.blind_dataset_sha256,
                candidate_fingerprint=report.candidate_fingerprint,
                profile_id=report.qwen_candidate.profile_id,
            )
            # 控制台只打印路径，不回显私有内容。
            print(f"本机私有诊断：{private_diagnostic_path}")
        # 私有文件写入后再次确认历史首次证据未被旁路修改。
        if frozen_result_snapshot is not None:
            # 两次快照把整个回归与写盘阶段都包在保护范围内。
            _assert_frozen_result_unchanged(
                FROZEN_RESULT_PATH,
                frozen_result_snapshot,
            )
    # 总是显示本机完整脱敏报告。
    print(f"报告：{REPORT_PATH}")
    # 只有真实候选通过质量门才返回零。
    return 0 if report.qwen_candidate.quality_gate_passed else 1


async def _async_main() -> int:
    """校验模式、取得首次运行锁，再执行计划、盲测或回归。"""

    # 读取命令行确认状态。
    args = _parse_args()
    # 四道参数关系必须在配置、私有题、.env和模型客户端之前成立。
    _validate_argument_contract(args)
    # 只有“首次付费”需要争夺独占锁；REGRESSION已有不可变首次结果。
    lock_first_paid_run = args.confirm_paid_api and not args.regression
    # 锁覆盖Settings读取、客户端创建、全部付费调用和首次结果发布。
    with _exclusive_first_paid_run_lock(enabled=lock_first_paid_run):
        # 取得锁后再次检查，关闭两个进程在参数校验后同时前进的竞态窗口。
        if lock_first_paid_run and FROZEN_RESULT_PATH.is_file():
            # 第二个进程不得产生任何额外模型费用。
            raise RuntimeError("首次冻结结果已由另一个进程创建，本进程停止")
        # 所有边界成立后才进入真正实验流程。
        return await _run_validated(args)


def main() -> int:
    """为PyCharm和普通Python入口创建异步事件循环。"""

    # asyncio.run负责事件循环创建与关闭。
    return asyncio.run(_async_main())


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit把真实候选Gate映射成进程退出码。
    raise SystemExit(main())
