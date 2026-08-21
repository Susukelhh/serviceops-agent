"""验证第26步开发选型、候选闭包和排序holdout默认隔离。"""

# Path为版本化排序实验配置提供明确类型。
from pathlib import Path

# PROJECT_ROOT确保测试从任意目录启动都读取同一配置。
from serviceops_agent.config.paths import PROJECT_ROOT

# 排序实验加载器和运行器是本文件验证的公共接口。
from serviceops_agent.evaluation import (
    load_rag_rerank_experiment_config,
    run_rag_rerank_experiment,
)

# CONFIG_PATH与第26步PyCharm脚本使用同一契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_rerank_experiment.json"


def test_rerank_development_selects_frozen_candidate_without_changing_candidates() -> None:
    """开发优胜者应提升Top-1、保持Recall，并且只改变候选顺序。"""

    # 加载已写入开发优胜名称的配置。
    config = load_rag_rerank_experiment_config(CONFIG_PATH)
    # 默认只运行开发集，自动测试不读取排序holdout。
    report = run_rag_rerank_experiment(config)
    # 按Profile名称建立稳定映射。
    results_by_id = {
        # 使用稳定名称作为键。
        result.profile_id: result
        # 遍历Baseline与全部权重候选。
        for result in report.development_results
    }
    # 原Qdrant顺序作为对照。
    baseline = results_by_id["vector-order-baseline"]
    # 冻结候选来自开发集选择，不来自holdout。
    candidate = results_by_id[config.frozen_candidate_profile_id]

    # 候选必须保持所有正例仍在Top-5内。
    assert candidate.metrics.recall_at_k == baseline.metrics.recall_at_k
    # Top-1必须相对原序获得预先要求的真实提升。
    assert candidate.metrics.top_1_accuracy > baseline.metrics.top_1_accuracy
    # MRR同步提高，说明不是只碰巧修正一条首位。
    assert candidate.metrics.mrr_at_k > baseline.metrics.mrr_at_k
    # 每条样本的候选文档集合保持相同，证明只重新排序。
    assert candidate.candidate_set_violation_case_ids == []
    # 候选通过开发联合质量门。
    assert candidate.quality_gate_passed is True
    # 选择算法与冻结配置必须一致。
    assert report.selected_profile_id == config.frozen_candidate_profile_id
    assert report.frozen_profile_matches_selection is True
    # 默认运行绝不消费排序锁定集。
    assert report.holdout_baseline is None
    assert report.holdout_candidate is None
    # 本实验完全本地，不产生模型费用。
    assert report.paid_api_called is False
