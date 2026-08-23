"""验证第31步四路对照、开发选择和默认锁定隔离。"""

from pathlib import Path

import pytest

from serviceops_agent.config.paths import PROJECT_ROOT
from serviceops_agent.evaluation.rag_hybrid_experiment import (
    load_rag_hybrid_experiment_config,
    run_rag_hybrid_experiment,
)

# 测试和 PyCharm 示例共用同一版本化实验契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_hybrid_experiment.json"


def test_hybrid_development_compares_four_routes_without_reading_holdout() -> None:
    """开发运行应有四类路线、真实相对指标，并且默认不产生 holdout 结果。"""

    config = load_rag_hybrid_experiment_config(CONFIG_PATH)
    report = run_rag_hybrid_experiment(config)
    modes = {result.mode for result in report.development_results}

    assert modes == {"dense_only", "candidate_bm25", "lexical_only", "hybrid_rrf"}
    assert report.development_case_count == 74
    assert report.selected_profile_id is not None
    assert report.holdout_results is None
    assert report.holdout_candidate is None
    assert report.paid_api_called is False


def test_hybrid_selected_candidate_improves_dense_ranking_without_recall_loss() -> None:
    """开发优胜 RRF 应保持 Recall，并提升 Top-1 和 MRR，而不是只增加技术名词。"""

    config = load_rag_hybrid_experiment_config(CONFIG_PATH)
    report = run_rag_hybrid_experiment(config)
    results = {result.profile_id: result for result in report.development_results}
    dense = results["dense-only-baseline"]
    candidate = results[report.selected_profile_id or ""]

    assert candidate.metrics.recall_at_k == dense.metrics.recall_at_k
    assert candidate.metrics.top_1_accuracy > dense.metrics.top_1_accuracy
    assert candidate.metrics.mrr_at_k > dense.metrics.mrr_at_k
    assert candidate.quality_gate_passed is True
    # 当前小语料 Dense@5 已全召回，因此本次收益应诚实表述为排序提升，不伪造救回数。
    assert report.lexical_rescue_case_ids == []


def test_hybrid_holdout_is_blocked_when_profile_is_not_frozen() -> None:
    """即使调用方确认holdout，开发优胜名称未冻结时也必须在读取前停止。"""

    config = load_rag_hybrid_experiment_config(CONFIG_PATH).model_copy(
        update={"frozen_candidate_profile_id": "PENDING_DEVELOPMENT_SELECTION"}
    )
    with pytest.raises(ValueError, match="冻结 Profile 不一致"):
        run_rag_hybrid_experiment(config, include_holdout=True)
