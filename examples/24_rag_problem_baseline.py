"""第24步第一阶段：运行不会调用千问的RAG困难问题Baseline。

运行方式：

    uv run python examples/24_rag_problem_baseline.py

本脚本的成功标准不是检索满分，而是语料规模达标且旧Hash方案至少暴露一个真实问题。
"""

# json 把强类型报告写成可审计、可用于后续候选对比的UTF-8文件。
import json

# Path 为命令行默认配置和报告路径提供明确类型。
from pathlib import Path

# PROJECT_ROOT 让脚本从PyCharm或任意终端目录启动时都使用同一项目文件。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第24步加载器和运行器负责配置校验、真实检索、失败归因与实验契约。
from serviceops_agent.evaluation import (
    RAGBaselineIssue,
    load_rag_problem_baseline_config,
    run_rag_problem_baseline,
)

# CONFIG_PATH 是受版本控制的困难Baseline实验契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_baseline_experiment.json"
# REPORT_PATH 位于git忽略的运行目录，避免每次时间戳变化污染提交。
REPORT_PATH: Path = PROJECT_ROOT / "data/runtime/rag_v2_problem_baseline_report.json"


def main() -> int:
    """执行离线Baseline，打印有限问题摘要，并返回可用于CI的退出码。"""

    # 加载配置会在搜索前校验所有路径字符串、阈值和离线Backend类型。
    config = load_rag_problem_baseline_config(CONFIG_PATH)
    # 运行器创建全新内存Qdrant并执行完整开发集，不读取本机千问配置。
    report = run_rag_problem_baseline(config)
    # runtime目录可能在全新克隆中不存在，因此显式创建父目录。
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # model_dump使用json模式把datetime和枚举转换为标准JSON值。
    report_payload = report.model_dump(mode="json")
    # ensure_ascii=False保持中文原因可读；indent=2便于面试时现场查看。
    REPORT_PATH.write_text(
        # 末尾换行符合版本化文本工具和终端查看习惯。
        json.dumps(report_payload, ensure_ascii=False, indent=2) + "\n",
        # 明确UTF-8避免Windows默认编码造成乱码。
        encoding="utf-8",
    )

    # 打印实验标题，提醒用户本轮不是优化候选。
    print("=== ServiceOps RAG v2 问题Baseline ===")
    # 输出版本和固定Profile，保证指标不会脱离参数解释。
    print(f"实验：{report.experiment_id} v{report.experiment_version}")
    # 明确付费边界，运行本脚本不消耗千问额度。
    print(f"方案：{report.baseline_profile_id}；付费API：否")
    # 规模行证明本轮不再是4个Chunk、11道题。
    print(
        f"规模：原始文档 {report.total_documents}，公共活动文档 "
        f"{report.indexable_documents}，实际Chunk {report.chunk_count}"
    )
    # 开发集与锁定集分开显示，提醒后者本轮没有运行。
    print(
        f"数据：开发集 {report.development_case_count}，"
        f"锁定集 {report.holdout_case_count}（未运行）"
    )
    # 依次输出统一检索指标，后续候选可以使用同一口径对比。
    print(f"Recall@{report.metrics.top_k}: {report.metrics.recall_at_k:.3f}")
    # MRR突出首个相关文档排名。
    print(f"MRR@{report.metrics.top_k}: {report.metrics.mrr_at_k:.3f}")
    # Top-1直接对应最终不重排时给模型的首条证据质量。
    print(f"Top-1准确率：{report.metrics.top_1_accuracy:.3f}")
    # nDCG同时观察相关性与排名位置。
    print(f"nDCG@{report.metrics.top_k}: {report.metrics.ndcg_at_k:.3f}")
    # 负例误召回率帮助防止单纯降低阈值换取Recall。
    print(f"负例误召回率：{report.metrics.false_positive_rate:.3f}")
    # 真实失败和排序机会分开，避免把所有非Top-1问题混为一类。
    print(
        f"决策失败：{report.failed_case_count}；"
        f"排序优化机会：{report.ranking_opportunity_count}"
    )

    # 只打印需要行动的样本，成功样本不制造终端噪声。
    print("\n=== 暴露的问题 ===")
    # 逐条遍历稳定诊断顺序。
    for diagnosis in report.diagnoses:
        # PASSED没有进一步根因分析价值，直接跳过。
        if diagnosis.issue == RAGBaselineIssue.PASSED:
            # continue进入下一条诊断。
            continue
        # 输出ID、类型、实际排名和首个相关位置，不泄漏知识正文。
        print(
            f"- {diagnosis.case_id}: {diagnosis.issue.value}; "
            f"retrieved={diagnosis.retrieved_document_ids}; "
            f"first_relevant_rank={diagnosis.first_relevant_rank}"
        )

    # 报告路径使用绝对值，方便用户从PyCharm控制台直接定位。
    print(f"\n报告：{REPORT_PATH}")
    # 契约通过表示困难基线成功暴露问题，脚本正常退出。
    if report.experiment_contract_passed:
        # 清楚解释“非满分”在本阶段为什么是PASS。
        print("实验设计门：PASS（旧方案已暴露可复现问题）")
        # 零退出码表示脚本本身和实验设计成功，不表示Baseline晋级。
        return 0
    # 输出所有失败原因，通常意味着语料太少或Baseline仍然满分。
    print(
        "实验设计门：FAIL（"
        + ", ".join(report.experiment_contract_failures)
        + "）"
    )
    # 非零退出码防止无效困难集被误当成后续优化依据。
    return 1


# 直接运行脚本时把main返回值交给Python进程。
if __name__ == "__main__":
    # SystemExit让PyCharm明确显示成功0或失败1。
    raise SystemExit(main())
