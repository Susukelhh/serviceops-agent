"""验证第24步困难基线的规模、隔离边界和可复现实验结论。"""

# Path 为测试中的版本化配置文件提供明确类型。
from pathlib import Path

# PROJECT_ROOT 确保测试不依赖pytest启动目录。
from serviceops_agent.config.paths import PROJECT_ROOT

# 基线问题枚举、加载器和运行器共同构成被验证的实验接口。
from serviceops_agent.evaluation import (
    RAGBaselineIssue,
    load_rag_problem_baseline_config,
    run_rag_problem_baseline,
)

# CONFIG_PATH 指向与教学脚本完全相同的实验契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_baseline_experiment.json"


def test_problem_baseline_exposes_reproducible_failures_without_paid_api() -> None:
    """旧Hash方案必须暴露问题，而不是在简单数据上再次取得虚假满分。"""

    # 配置加载阶段先执行Pydantic结构与范围校验。
    config = load_rag_problem_baseline_config(CONFIG_PATH)
    # 每次运行创建独立内存Qdrant，结果不受本地历史索引污染。
    report = run_rag_problem_baseline(config)

    # 第一阶段明确不允许调用千问或其他付费Embedding接口。
    assert report.paid_api_called is False
    # 语料、Chunk、正负例和Baseline失败均满足版本化实验契约。
    assert report.experiment_contract_passed is True
    # 原始语料同时包含公开文档、内部草稿与退役政策。
    assert report.total_documents >= config.contract.min_total_documents
    # 真正进入索引的只有published + public文档。
    assert report.indexable_documents >= config.contract.min_indexable_documents
    # 默认字符窗口已经实际产生多于文档数的切片。
    assert report.chunk_count >= config.contract.min_chunks
    # 旧方案至少产生一个真实失败，否则困难集没有完成任务。
    assert report.failed_case_count >= config.contract.min_baseline_failures
    # 高词面重合负例证明仅靠较低向量阈值会把域外问题误当成证据。
    assert report.metrics.false_positive_rate > 0.0
    # 至少存在一条相邻政策排序机会，后续Rerank才有明确引入理由。
    assert report.ranking_opportunity_count > 0


def test_problem_baseline_never_indexes_internal_or_retired_documents() -> None:
    """治理过滤必须早于Embedding，敏感和失效文档不能出现在任意排名。"""

    # 复用同一版本化配置，避免测试私自放宽知识治理条件。
    config = load_rag_problem_baseline_config(CONFIG_PATH)
    # 运行完整开发集后检查所有实际检索文档ID。
    report = run_rag_problem_baseline(config)
    # 汇总每条诊断中的去重文档ID，便于执行全局泄漏断言。
    retrieved_document_ids = {
        # 取出当前命中的父文档ID。
        document_id
        # 遍历全部开发样本诊断。
        for diagnosis in report.diagnoses
        # 遍历单条样本实际排名。
        for document_id in diagnosis.retrieved_document_ids
    }

    # 内部VIP规则即使查询精确命中标题，也不允许进入公共索引。
    assert "KB-INTERNAL-VIP-001" not in retrieved_document_ids
    # 已废止十五天规则即使词面高度相关，也不允许成为回答证据。
    assert "KB-RETURN-RETIRED-001" not in retrieved_document_ids


def test_problem_baseline_reports_issue_categories_and_keeps_holdout_locked() -> None:
    """报告应提供可行动诊断，同时不在调参阶段执行锁定测试集。"""

    # 读取稳定配置。
    config = load_rag_problem_baseline_config(CONFIG_PATH)
    # 运行本阶段允许使用的开发集。
    report = run_rag_problem_baseline(config)
    # 汇总有限诊断类型，检查实验确实区分决策失败与排序机会。
    issue_types = {diagnosis.issue for diagnosis in report.diagnoses}

    # 至少包含负例误召回，后续需要拒答门或阈值优化。
    assert RAGBaselineIssue.IRRELEVANT_EVIDENCE_RETURNED in issue_types
    # 至少包含相关文档排名靠后，后续可以有依据地比较Rerank。
    assert RAGBaselineIssue.RELEVANT_DOCUMENT_RANKED_LOW in issue_types
    # 报告只对开发集生成逐样本诊断。
    assert len(report.diagnoses) == report.development_case_count
    # 锁定集只校验数量，不应该混入当前诊断结果。
    assert report.holdout_case_count > 0
    # 所有当前诊断ID都必须来自dev命名空间，证明holdout没有被执行。
    assert all(diagnosis.case_id.startswith("dev-") for diagnosis in report.diagnoses)
