"""第35步：在已揭晓开发集上校准评分器，并验证Grounded回答提示v2。

本文件明确不是新盲测。默认只展示计划；零费用重放复用第34步已经保存的模型回答；只有同时确认
已揭晓回归和付费API后，才会运行新的Prompt v2候选。
"""

# argparse提供已揭晓回归、零费用重放、付费候选和私有诊断四个显式开关。
import argparse

# asyncio运行异步回放评分和真实候选。
import asyncio

# json只序列化脱敏Summary或Report，不写入问题和模型答案。
import json

# os提供完整刷盘与同目录原子替换。
import os

# Path定位公开配置、私有诊断目录和runtime报告。
from pathlib import Path

# uuid4保证临时文件名不会和并发进程冲突。
from uuid import uuid4

# PROJECT_ROOT让PyCharm从任意Working directory运行时都能定位项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings只在用户明确允许付费后读取.env中的千问配置。
from serviceops_agent.config.settings import Settings

# 第35步复用第34步检索、回答和单一指标，只替换显式版本化Profile与Prompt。
from serviceops_agent.evaluation import (
    GroundedAnswerSuccessSummary,
    PrivateGroundedAnswerDiagnosticCollector,
    grounded_answer_candidate_fingerprint,
    load_grounded_answer_development_scoring_profile,
    load_grounded_answer_success_config,
    load_private_grounded_answer_diagnostic_collector,
    replay_private_grounded_answer_diagnostics,
    run_grounded_answer_success_experiment,
    write_private_grounded_answer_diagnostics,
)

# DEVELOPMENT_CONFIG_PATH冻结Prompt v2、模型、检索参数和同一已揭晓题集摘要。
DEVELOPMENT_CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_v2_development_experiment.json"
)
# SCORING_PROFILE_PATH只保存人工审计后的事实ID与同义扩展，不含问题或模型答案。
SCORING_PROFILE_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_development_scoring_v1_1.json"
)
# SOURCE_V1_CONFIG_PATH提供原v1候选指纹，用于校验零费用重放没有偷换原回答。
SOURCE_V1_CONFIG_PATH: Path = (
    PROJECT_ROOT / "data/evaluation/grounded_answer_success_experiment.json"
)
# PRIVATE_DIAGNOSTIC_DIRECTORY与第34步writer固定目录完全一致，且被Git和Docker忽略。
PRIVATE_DIAGNOSTIC_DIRECTORY: Path = (
    PROJECT_ROOT
    / "data/private_evaluation/diagnostics/grounded_answer_success"
)
# REPLAY_REPORT_PATH保存零费用重放的脱敏逐题结果。
REPLAY_REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v1_1_replay_report.json"
)
# DEVELOPMENT_REPORT_PATH保存Prompt v2真实开发回归报告。
DEVELOPMENT_REPORT_PATH: Path = (
    PROJECT_ROOT / "data/runtime/grounded_answer_v2_development_report.json"
)


def _parse_args() -> argparse.Namespace:
    """解析已揭晓数据确认、零费用重放和真实付费候选开关。"""

    # 默认模式不读取私有题目、诊断或API Key。
    parser = argparse.ArgumentParser(
        description="运行第35步已揭晓开发回归；它不是新的盲测。"
    )
    # 第一把钥匙确认用户理解v1题目已经揭晓，只能作为开发集。
    parser.add_argument(
        "--confirm-revealed-regression",
        action="store_true",
        help="确认使用已揭晓v1题集进行开发回归，不宣称新盲测。",
    )
    # 零费用模式读取最新v1私有诊断，重放原回答并应用v1.1评分器。
    parser.add_argument(
        "--replay-latest-v1-diagnostic",
        action="store_true",
        help="零费用重放最新v1私有诊断，不调用Embedding或聊天模型。",
    )
    # 第二把钥匙允许Prompt v2发起真实千问Embedding和聊天调用。
    parser.add_argument(
        "--confirm-paid-api",
        action="store_true",
        help="确认在已揭晓开发集上调用真实千问Prompt v2候选。",
    )
    # 只有付费v2运行时才允许保存新一轮私有问题、证据和答案。
    parser.add_argument(
        "--write-private-diagnostics",
        action="store_true",
        help="为付费Prompt v2回归保存本机私有诊断。",
    )
    # 返回argparse生成的简单布尔参数对象。
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    """在读取私有目录、.env或模型配置前校验所有模式关系。"""

    # 任何读取已揭晓正文或产生费用的操作都必须先承认它不是盲测。
    if (
        args.replay_latest_v1_diagnostic
        or args.confirm_paid_api
        or args.write_private_diagnostics
    ) and not args.confirm_revealed_regression:
        # 固定错误不包含文件路径或Key。
        raise ValueError("必须先提供--confirm-revealed-regression")
    # 重放与新生成是两个不同实验，禁止在一次命令中混用。
    if args.replay_latest_v1_diagnostic and args.confirm_paid_api:
        # 用户应先零费用确认评分器，再单独决定是否运行付费v2。
        raise ValueError("零费用重放不能与--confirm-paid-api同时使用")
    # 私有v2诊断必须依赖真实v2候选，不能为离线基线创建误导文件。
    if args.write_private_diagnostics and not args.confirm_paid_api:
        # 在创建Collector或目录前停止。
        raise ValueError("--write-private-diagnostics必须同时确认付费API")


def _write_json_atomic(path: Path, payload: object) -> None:
    """先把完整JSON写入同目录临时文件，再原子替换runtime报告。"""

    # runtime目录在新克隆中可能尚不存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件和目标同目录，保证os.replace不会跨磁盘分区。
    temporary_path = path.parent / f".{path.name}.{uuid4().hex}.partial"
    # 在内存先完成序列化，失败时不会留下任何文件。
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        # x模式避免极小概率UUID碰撞时覆盖其他进程的临时文件。
        with temporary_path.open("x", encoding="utf-8", newline="\n") as handle:
            # 一次写入完整UTF-8 JSON。
            handle.write(content)
            # 把Python缓冲区交给操作系统。
            handle.flush()
            # 要求内容完整落盘后再发布最终路径。
            os.fsync(handle.fileno())
        # runtime允许新一轮完整报告替换旧报告，但不会出现半份JSON。
        os.replace(temporary_path, path)
    finally:
        # 写入或发布失败时删除partial文件。
        temporary_path.unlink(missing_ok=True)


def _latest_v1_diagnostic(*, expected_candidate_fingerprint: str) -> Path:
    """返回固定私有目录中最新一份v1候选REGRESSION诊断。"""

    # 目录不存在表示用户尚未完成第34步私有回归。
    if not PRIVATE_DIAGNOSTIC_DIRECTORY.is_dir():
        # 不打印绝对用户目录。
        raise FileNotFoundError("尚未找到第34步私有诊断目录")
    # 文件名包含候选指纹前12位；过滤后即使已有v2诊断也不会误选新候选。
    filename_pattern = (
        f"*_{expected_candidate_fingerprint[:12]}_*_REGRESSION.json"
    )
    # 文件名以UTC时间开头，按名称排序即可得到最新一轮v1运行。
    candidates = sorted(
        PRIVATE_DIAGNOSTIC_DIRECTORY.glob(filename_pattern),
        reverse=True,
    )
    # 空目录不能进行离线重放。
    if not candidates:
        # 提示先完成第34步诊断。
        raise FileNotFoundError("尚未找到可重放的第34步REGRESSION诊断")
    # loader还会再次验证路径边界、题集SHA和候选指纹。
    return candidates[0]


def _print_summary(title: str, summary: GroundedAnswerSuccessSummary) -> None:
    """只展示单一成功率、红线和脱敏失败ID。"""

    # 标题说明当前是回放还是新候选。
    print(f"\n{title}：")
    # 主结论始终包含分子和固定分母。
    print(
        "端到端有据回答成功率："
        f"{summary.passed_cases}/{summary.total_cases} = "
        f"{summary.grounded_answer_success_rate:.2%}"
    )
    # 红线仍是零容忍否决，不包装成第二个平均指标。
    print(f"红线失败题：{len(summary.red_line_case_ids)}条")
    # 即使达到80%，存在红线时也必须显示FAIL。
    print(f"质量门：{'PASS' if summary.quality_gate_passed else 'FAIL'}")
    # 只显示稳定ID和有限原因码。
    for result in summary.results:
        # 通过题不产生额外输出噪声。
        if result.passed:
            # 继续下一题。
            continue
        # 不打印问题、答案或事实规则正文。
        print(f"- {result.case_id}：{','.join(result.failure_codes)}")


async def _run_replay() -> int:
    """零费用重放最新v1原回答，并应用v1.1开发评分Profile。"""

    # 三份公开配置都不含私有问题或模型答案。
    development_config = load_grounded_answer_success_config(
        DEVELOPMENT_CONFIG_PATH
    )
    # v1配置提供必须匹配的原候选指纹。
    source_v1_config = load_grounded_answer_success_config(SOURCE_V1_CONFIG_PATH)
    # Profile包含人工审计后的排除项和同义扩展。
    scoring_profile = load_grounded_answer_development_scoring_profile(
        SCORING_PROFILE_PATH
    )
    # 先计算原v1指纹，既用于文件名过滤，也用于loader完整校验。
    source_candidate_fingerprint = grounded_answer_candidate_fingerprint(
        source_v1_config
    )
    # 只在显式replay模式查找匹配原v1候选的私有目录文件。
    diagnostic_path = _latest_v1_diagnostic(
        expected_candidate_fingerprint=source_candidate_fingerprint
    )
    # loader校验固定私有根、题集SHA和原v1候选指纹。
    collector = load_private_grounded_answer_diagnostic_collector(
        diagnostic_path,
        expected_dataset_sha256=development_config.blind_dataset_sha256,
        expected_candidate_fingerprint=source_candidate_fingerprint,
    )
    # 回放只使用本地记录型替身，不创建Qdrant、Embedding或聊天客户端。
    summary = await replay_private_grounded_answer_diagnostics(
        collector,
        scoring_profile=scoring_profile,
        result_profile_id="qwen-v1-response-development-rescore-v1.1",
        top_k=development_config.top_k,
        min_success_rate=development_config.min_grounded_answer_success_rate,
        zero_tolerance_failure_codes=(
            development_config.zero_tolerance_failure_codes
        ),
    )
    # runtime报告只保存脱敏Summary和公开版本身份。
    _write_json_atomic(
        REPLAY_REPORT_PATH,
        {
            "run_kind": "REVEALED_DEVELOPMENT_REPLAY",
            "paid_api_called": False,
            "source_dataset_sha256": scoring_profile.source_dataset_sha256,
            "source_candidate_fingerprint": source_candidate_fingerprint,
            "scoring_profile_id": scoring_profile.profile_id,
            "summary": summary.model_dump(mode="json"),
            "contains_private_questions": False,
            "contains_model_answers": False,
            "contains_fact_rule_text": False,
        },
    )
    # 控制台明确这只是已揭晓开发重放。
    print("=== ServiceOps 第35步：已揭晓开发集零费用重放 ===")
    print("说明：复用第34步原回答；不调用千问；不能称为新盲测。")
    _print_summary("v1原回答 + v1.1开发评分器", summary)
    print(f"报告：{REPLAY_REPORT_PATH}")
    # 质量门失败返回1；这不代表脚本或回放过程异常。
    return 0 if summary.quality_gate_passed else 1


async def _run_v2_candidate(*, write_private_diagnostics: bool) -> int:
    """显式付费运行Prompt v2开发候选，并可选保存私有诊断。"""

    # 公开开发配置冻结Prompt v2候选身份。
    config = load_grounded_answer_success_config(DEVELOPMENT_CONFIG_PATH)
    # 加载v1.1开发评分Profile。
    scoring_profile = load_grounded_answer_development_scoring_profile(
        SCORING_PROFILE_PATH
    )
    # 只有显式私有开关才在内存保留问题、证据和答案。
    collector = (
        PrivateGroundedAnswerDiagnosticCollector()
        if write_private_diagnostics
        else None
    )
    # 此处才读取.env中的千问Key和Base URL。
    settings = Settings()
    # confirm_regression明确同一v1题集已经揭晓；它不是首次盲测。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
        confirm_blind=True,
        confirm_paid_api=True,
        confirm_regression=True,
        private_diagnostic_collector=collector,
        development_scoring_profile=scoring_profile,
    )
    # 脱敏Report可以安全写入runtime。
    _write_json_atomic(
        DEVELOPMENT_REPORT_PATH,
        report.model_dump(mode="json"),
    )
    # qwen_candidate在confirm_paid_api=True时一定存在；防御类型收窄。
    if report.qwen_candidate is None:
        # 固定错误不包含模型响应。
        raise RuntimeError("Prompt v2真实候选没有返回Summary")
    # 展示新候选唯一主指标。
    print("=== ServiceOps 第35步：Prompt v2已揭晓开发回归 ===")
    print("说明：这不是新盲测，不能替换第34步首次40%结果。")
    print(f"候选指纹：{report.candidate_fingerprint}")
    _print_summary("千问Prompt v2开发候选", report.qwen_candidate)
    print(
        f"实际Embedding请求：{report.actual_embedding_requests}次；"
        f"输入Token：{report.actual_embedding_input_tokens}；"
        f"实际聊天调用：{report.actual_chat_calls}次"
    )
    # 私有writer使用独立唯一文件，不覆盖v1诊断。
    if collector is not None:
        # 新文件会携带v2候选指纹。
        private_path = write_private_grounded_answer_diagnostics(
            collector,
            experiment_id=report.experiment_id,
            experiment_version=report.experiment_version,
            dataset_sha256=report.blind_dataset_sha256,
            candidate_fingerprint=report.candidate_fingerprint,
            profile_id=report.qwen_candidate.profile_id,
        )
        # 只打印路径，不回显正文。
        print(f"本机私有诊断：{private_path}")
    # 显示脱敏报告位置。
    print(f"报告：{DEVELOPMENT_REPORT_PATH}")
    # Gate映射为进程退出码。
    return 0 if report.qwen_candidate.quality_gate_passed else 1


async def _async_main() -> int:
    """按安全状态机执行计划、零费用重放或付费v2开发候选。"""

    # 参数解析不读取任何项目文件。
    args = _parse_args()
    # 非法组合在私有目录和.env之前失败。
    _validate_args(args)
    # 零费用重放使用已经保存的v1答案。
    if args.replay_latest_v1_diagnostic:
        # 不创建Settings或外部客户端。
        return await _run_replay()
    # 付费开关存在时运行新Prompt v2候选。
    if args.confirm_paid_api:
        # 私有诊断开关原样传入。
        return await _run_v2_candidate(
            write_private_diagnostics=args.write_private_diagnostics
        )
    # 默认或只确认revealed时都只展示公开计划，不读取私有正文。
    config = load_grounded_answer_success_config(DEVELOPMENT_CONFIG_PATH)
    # 计算公开候选身份不会访问模型。
    candidate_fingerprint = grounded_answer_candidate_fingerprint(config)
    # 输出最少但足够核对的开发计划。
    print("=== ServiceOps 第35步：Grounded回答Prompt v2开发计划 ===")
    print("当前数据：已揭晓v1回归集，不是新盲测。")
    print(f"评分器：{config.evaluator_version}")
    print(f"Prompt版本：{config.grounding_prompt_version}")
    print(f"候选指纹：{candidate_fingerprint}")
    print("零费用重放参数：--confirm-revealed-regression --replay-latest-v1-diagnostic")
    print("真实Prompt v2会再次产生约4次Embedding和最多25次聊天调用。")
    # 计划展示成功退出。
    return 0


def main() -> int:
    """为PyCharm和命令行创建并关闭异步事件循环。"""

    # asyncio.run统一管理事件循环。
    return asyncio.run(_async_main())


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit让质量门结论映射为进程退出码。
    raise SystemExit(main())
