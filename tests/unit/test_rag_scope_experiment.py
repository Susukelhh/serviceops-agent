"""验证第25步阈值取舍、范围门晋级和holdout默认隔离。"""

# Path为版本化实验配置提供明确类型。
from pathlib import Path

# PROJECT_ROOT避免测试依赖当前工作目录。
from serviceops_agent.config.paths import PROJECT_ROOT

# 第25步加载器和实验运行器是本文件验证的公共接口。
from serviceops_agent.evaluation import (
    load_rag_scope_experiment_config,
    run_rag_scope_experiment,
)

# CONFIG_PATH与PyCharm教学脚本使用同一份实验契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_scope_experiment.json"


def test_threshold_scan_proves_false_positive_and_recall_tradeoff() -> None:
    """单纯提高阈值虽能减少误召回，却不能同时守住正例召回。"""

    # 加载固定阈值列表和质量门。
    config = load_rag_scope_experiment_config(CONFIG_PATH)
    # 默认只运行开发集，避免自动化测试反复查看holdout。
    report = run_rag_scope_experiment(config)
    # 按Profile名称建立映射，断言不依赖列表偶然位置。
    results_by_id = {
        # 使用稳定Profile名称作为键。
        result.profile_id: result
        # 遍历全部开发候选。
        for result in report.development_results
    }
    # 低阈值Baseline保存完整正例Recall。
    low_threshold = results_by_id["threshold-only-0.10"]
    # 高阈值候选用于观察拒答增加后的副作用。
    high_threshold = results_by_id["threshold-only-0.30"]

    # Baseline仍然对所有负例错误返回证据。
    assert low_threshold.metrics.false_positive_rate == 1.0
    # 提高到0.30可以过滤全部当前负例。
    assert high_threshold.metrics.false_positive_rate == 0.0
    # 但正例Recall明显下降，证明阈值不是免费午餐。
    assert high_threshold.metrics.recall_at_k < low_threshold.metrics.recall_at_k
    # 因此两个纯阈值方案都不能通过三项联合质量门。
    assert low_threshold.quality_gate_passed is False
    assert high_threshold.quality_gate_passed is False


def test_scope_gate_improves_decision_without_reducing_development_recall() -> None:
    """范围门应解决负例误召回，同时保留Baseline正例Recall。"""

    # 读取受版本控制配置。
    config = load_rag_scope_experiment_config(CONFIG_PATH)
    # 运行开发集候选选择。
    report = run_rag_scope_experiment(config)
    # 使用名称映射取得Baseline和冻结范围门候选。
    results_by_id = {
        # 当前结果名称作为稳定键。
        result.profile_id: result
        # 遍历全部开发结果。
        for result in report.development_results
    }
    # 第一个阈值Profile是冻结旧方案。
    baseline = results_by_id["threshold-only-0.10"]
    # 候选名称来自运行holdout前已写入配置的固定值。
    candidate = results_by_id[config.frozen_candidate_profile_id]

    # 范围门不降低知识内问题Recall。
    assert candidate.metrics.recall_at_k == baseline.metrics.recall_at_k
    # 范围门将开发集负例误召回降到零。
    assert candidate.metrics.false_positive_rate == 0.0
    # 整体正负决策从Baseline的75%提高到100%。
    assert candidate.metrics.decision_accuracy > baseline.metrics.decision_accuracy
    # 候选通过预先声明的联合质量门。
    assert candidate.quality_gate_passed is True
    # 开发集算法选择结果与冻结名称一致，具备运行holdout的前置条件。
    assert report.selected_profile_id == config.frozen_candidate_profile_id
    assert report.frozen_profile_matches_selection is True


def test_development_experiment_does_not_run_holdout_by_default() -> None:
    """普通脚本和自动测试不能在调参阶段反复消费锁定集。"""

    # 加载同一实验配置。
    config = load_rag_scope_experiment_config(CONFIG_PATH)
    # 不传include_holdout，保持显式默认False。
    report = run_rag_scope_experiment(config)

    # 报告必须明确没有锁定结果。
    assert report.holdout_result is None
    # 所有候选结果都来自development，不允许混入holdout样本。
    assert all(
        # 当前Profile必须标记为开发集。
        result.dataset == "development"
        # 遍历全部候选结果。
        for result in report.development_results
    )
    # 本实验完全离线，不调用真实Embedding或聊天模型。
    assert report.paid_api_called is False
