"""第34步：运行事实级端到端有据回答成功率盲测。"""

# argparse提供私有盲测、真实付费候选和已揭晓回归三道显式开关。
import argparse

# asyncio负责运行异步Grounded回答器。
import asyncio

# json把脱敏强类型报告保存为UTF-8文件。
import json

# Path声明稳定配置、运行报告和公开冻结结果路径。
from pathlib import Path

# PROJECT_ROOT保证PyCharm从任意Working directory启动都能定位项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings读取.env，但默认和离线盲测路径都不会使用API Key。
from serviceops_agent.config.settings import Settings

# 第34步公共类型与运行器是本脚本的唯一业务依赖。
from serviceops_agent.evaluation import (
    GroundedAnswerSuccessSummary,
    load_grounded_answer_success_config,
    run_grounded_answer_success_experiment,
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


def _parse_args() -> argparse.Namespace:
    """解析三道明确确认开关。"""

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
    # 返回命令行参数对象。
    return parser.parse_args()


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
    # 结果目录在旧实验中通常已存在，但新环境仍显式创建。
    FROZEN_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 中文保持可读，末尾换行便于Git diff。
    FROZEN_RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _async_main() -> int:
    """执行计划、离线揭晓或显式付费的真实冻结候选。"""

    # 读取命令行确认状态。
    args = _parse_args()
    # 付费候选必须同时确认私有盲测读取。
    if args.confirm_paid_api and not args.confirm_blind:
        # 在任何文件或网络访问前快速失败。
        raise ValueError("--confirm-paid-api必须同时提供--confirm-blind")
    # regression只对付费复跑有意义，避免用户误以为普通离线运行改变历史。
    if args.regression and not args.confirm_paid_api:
        # 给出明确参数关系。
        raise ValueError("--regression必须同时提供--confirm-paid-api")
    # 已存在首次真实结果时，默认禁止再次把同一题集叫盲测。
    if args.confirm_paid_api and FROZEN_RESULT_PATH.exists() and not args.regression:
        # 用户必须显式承认这是回归复跑。
        raise ValueError("首次真实盲测结果已存在；复跑请添加--regression")
    # 加载不含私有正文的公开配置。
    config = load_grounded_answer_success_config(CONFIG_PATH)
    # Settings只在真实候选路径读取Key值。
    settings = Settings()
    # 执行对应确认级别的实验。
    report = await run_grounded_answer_success_experiment(
        config,
        runtime_settings=settings,
        confirm_blind=args.confirm_blind,
        confirm_paid_api=args.confirm_paid_api,
    )
    # runtime报告永不进入Git。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Pydantic JSON模式确保所有字段可序列化。
    REPORT_PATH.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
    # 总是显示本机完整脱敏报告。
    print(f"报告：{REPORT_PATH}")
    # 只有真实候选通过质量门才返回零。
    return 0 if report.qwen_candidate.quality_gate_passed else 1


def main() -> int:
    """为PyCharm和普通Python入口创建异步事件循环。"""

    # asyncio.run负责事件循环创建与关闭。
    return asyncio.run(_async_main())


# PyCharm直接运行本文件时进入main。
if __name__ == "__main__":
    # SystemExit把真实候选Gate映射成进程退出码。
    raise SystemExit(main())
