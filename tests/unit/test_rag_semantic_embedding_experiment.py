"""验证第27步离线基线、费用保护和锁定集隔离。"""

# Path为版本化实验配置提供明确类型。
from pathlib import Path

# PROJECT_ROOT保证测试从任意工作目录读取同一配置。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings使用mock聊天和Hash默认值，不需要任何真实密钥。
from serviceops_agent.config.settings import Settings

# 第27步加载器与运行器是本测试验证的公共接口。
from serviceops_agent.evaluation import (
    load_rag_semantic_embedding_experiment_config,
    run_rag_semantic_embedding_experiment,
)

# CONFIG_PATH与PyCharm示例脚本共用同一版本化契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_v2_semantic_embedding_experiment.json"


def test_semantic_experiment_exposes_hash_problem_without_paid_api() -> None:
    """默认运行应暴露Hash召回/拒答冲突，同时绝不调用千问。"""

    # 加载真实项目实验配置。
    config = load_rag_semantic_embedding_experiment_config(CONFIG_PATH)
    # 真实开发集已经选出唯一通过质量门的0.50，holdout前必须冻结该值。
    assert config.frozen_candidate_threshold == 0.50
    # 显式传入无密钥设置，证明离线路径不依赖.env中的个人Key。
    settings = Settings(
        # 测试不调用聊天模型。
        llm_backend="mock",
        # 清空模型密钥。
        llm_api_key=None,
        # 清空兼容地址。
        llm_base_url=None,
        # 测试遥测关闭，减少无关输出。
        telemetry_enabled=False,
    )
    # 不提供confirm_paid_api，费用门必须关闭。
    report = run_rag_semantic_embedding_experiment(
        config,
        runtime_settings=settings,
    )

    # 报告证明真实候选没有运行。
    assert report.paid_api_called is False
    # 没有任何真实请求或Token费用。
    assert report.actual_api_requests == 0
    assert report.actual_input_tokens == 0
    assert report.actual_cost_cny == 0.0
    # 千问结果为空，不能把未运行误报成0分。
    assert report.qwen_development_results == []
    assert report.qwen_selected_threshold is None
    # 锁定结果默认完全隔离。
    assert report.hash_holdout is None
    assert report.qwen_holdout is None

    # Hash专项集确实暴露短板，而不是又得到没有诊断价值的100%。
    selected_hash = next(
        result
        for result in report.hash_development_results
        if result.score_threshold == report.hash_selected_threshold
    )
    # Recall与综合决策至少一项应低于真实候选预设门。
    assert (
        selected_hash.metrics.recall_at_k < config.development_gate.min_recall_at_k
        or selected_hash.metrics.decision_accuracy < config.development_gate.min_decision_accuracy
    )
    # Hash最佳点不应通过真实语义候选质量门。
    assert selected_hash.quality_gate_passed is False
    # 当前23切片和16问题按20条分批，计划请求固定为3次。
    assert report.planned_development_api_requests == 3
    # 10条holdout问题只需增加一批，且本测试没有读取或运行它。
    assert report.planned_holdout_extra_api_requests == 1
