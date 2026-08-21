"""第十四步真实候选重复实验的隔离集成测试。"""

# pytest 提供异步测试标记和异常断言。
import pytest

# Pydantic ValidationError 证明矛盾阈值在模型调用前失败。
from pydantic import ValidationError

# Settings 用于验证真实候选缺少 API Key 时快速失败且不发网络请求。
from serviceops_agent.config.settings import Settings

# 端到端评测与候选实验公共入口是本文件的被测对象。
from serviceops_agent.evaluation import (
    QWEN_CANDIDATE_PROFILE,
    AgentCandidateExperimentConfig,
    CandidatePromotionThresholds,
    build_offline_agent_evaluation_target,
    build_qwen_candidate_evaluation_target,
    estimate_planned_qwen_chat_calls,
    evaluate_agent_dataset,
    load_agent_evaluation_dataset,
    load_candidate_experiment_config,
    override_candidate_trial_count,
    run_candidate_experiment,
    summarize_candidate_experiment,
)


# 第一条测试使用确定性目标替身重复两轮，验证完整聚合链路不依赖付费服务。
@pytest.mark.asyncio
async def test_repeated_candidate_experiment_passes_with_stable_target() -> None:
    """相同稳定目标跨两轮应得到 100% 稳定率并通过晋级门。"""

    # Arrange：加载与真实脚本相同的受版本控制黄金集和候选实验配置。
    dataset = load_agent_evaluation_dataset(
        "data/evaluation/agent_end_to_end_cases.json"
    )
    raw_config = load_candidate_experiment_config(
        "data/evaluation/qwen_candidate_experiment.json"
    )
    # 分类提示和事件契约变更后实验升级到 1.1.0，不能与首次 1.0.0 报告混为同一处理组。
    assert raw_config.version == "1.1.0"
    # 测试覆盖两轮即可证明重复聚合，减少自动化运行时间。
    config = override_candidate_trial_count(raw_config, 2)

    # Act：候选工厂故意使用离线稳定图，避免测试读取开发者 .env 或消耗千问额度。
    summary = await run_candidate_experiment(
        dataset=dataset,
        config=config,
        candidate_model="offline-test-double",
        candidate_target_factory=build_offline_agent_evaluation_target,
    )

    # Assert：严格离线基线必须先通过，候选才有比较意义。
    assert summary.baseline_summary.quality_gate_passed is True
    # 两轮完整结果都被保留，不能只保留最好一轮。
    assert len(summary.candidate_trials) == 2
    assert all(trial.overall_pass_rate == 1.0 for trial in summary.candidate_trials)
    # 当前 13 条参考路径每轮预计 24 次聊天模型请求，报告同时计算两轮总量。
    assert estimate_planned_qwen_chat_calls(dataset) == 24
    assert summary.planned_chat_calls_per_trial == 24
    assert summary.planned_total_chat_calls == 48
    # 每条黄金样本在两轮都通过，因此全轮稳定率和安全维度均为 100%。
    assert summary.fully_stable_cases == 13
    assert summary.fully_stable_case_rate == 1.0
    assert summary.mean_safety_invariant_accuracy == 1.0
    assert summary.promotion_gate_passed is True
    assert summary.promotion_gate_failures == []
    # 候选报告保留实际有限事件，真实模型失败时无需再次付费即可定位轨迹差异。
    assert summary.candidate_trials[0].results[0].actual_events
    assert (
        "graph:intent_classified_as_faq"
        in summary.candidate_trials[0].results[0].actual_events
    )


# 第二条测试故意让一轮中的一个 case 失败，证明平均数不会掩盖逐场景波动。
@pytest.mark.asyncio
async def test_candidate_promotion_gate_reports_intermittent_case() -> None:
    """单轮偶发路由失败必须降低均值、最差轮和全轮稳定样本率。"""

    # Arrange：先得到一份真实完整离线结果，避免手工伪造庞大 State 结构。
    dataset = load_agent_evaluation_dataset(
        "data/evaluation/agent_end_to_end_cases.json"
    )
    graph, repository = build_offline_agent_evaluation_target()
    baseline = await evaluate_agent_dataset(graph, repository, dataset)
    # 第一候选轮复用相同内容，但明确标为候选 profile。
    passed_trial = baseline.model_copy(
        update={"target_profile": QWEN_CANDIDATE_PROFILE}
    )
    # 第二轮把首个 case 改成路由失败，其他维度和安全结果保持不变。
    first_result = passed_trial.results[0]
    failed_first_result = first_result.model_copy(
        update={
            "routing_passed": False,
            "passed": False,
            "violations": ["intent_mismatch"],
        }
    )
    failed_trial = passed_trial.model_copy(
        update={
            "passed_cases": passed_trial.total_cases - 1,
            "overall_pass_rate": (passed_trial.total_cases - 1)
            / passed_trial.total_cases,
            "routing_accuracy": (passed_trial.total_cases - 1)
            / passed_trial.total_cases,
            "quality_gate_passed": False,
            "quality_gate_failures": ["overall_pass_rate_below_threshold"],
            "results": [failed_first_result, *passed_trial.results[1:]],
        }
    )
    # 使用严格 100% 门，让测试能精确断言三个稳定性失败码。
    config = AgentCandidateExperimentConfig(
        experiment_id="intermittent-test",
        version="1.0.0",
        description="测试单个候选 case 跨轮偶发失败的聚合行为。",
        candidate_profile=QWEN_CANDIDATE_PROFILE,
        trials=2,
        thresholds=CandidatePromotionThresholds(
            min_mean_overall_pass_rate=1.0,
            min_worst_trial_overall_pass_rate=1.0,
            min_fully_stable_case_rate=1.0,
            min_mean_safety_invariant_accuracy=1.0,
        ),
    )

    # Act：纯聚合器接收一轮通过、一轮偶发失败的候选结果。
    summary = summarize_candidate_experiment(
        dataset=dataset,
        config=config,
        candidate_model="unstable-test-double",
        baseline_summary=baseline,
        candidate_trials=[passed_trial, failed_trial],
    )

    # Assert：失败 case 的稳定率是 1/2，并保留可定位规则码。
    unstable_case = summary.case_stability[0]
    assert unstable_case.passed_trials == 1
    assert unstable_case.pass_rate == 0.5
    assert unstable_case.fully_stable is False
    assert unstable_case.observed_violations == ["intent_mismatch"]
    # 安全仍是 100%，所以失败门只来自整体均值、最差轮和稳定样本率。
    assert summary.mean_safety_invariant_accuracy == 1.0
    assert summary.promotion_gate_passed is False
    assert summary.promotion_gate_failures == [
        "mean_overall_pass_rate_below_threshold",
        "worst_trial_overall_pass_rate_below_threshold",
        "fully_stable_case_rate_below_threshold",
    ]


# 第三条测试验证真实目标在创建聊天客户端时就检查密钥，不会等到半轮实验后才报错。
def test_qwen_candidate_target_rejects_missing_api_key() -> None:
    """缺少千问 API Key 必须在任何模型网络请求前快速失败。"""

    # Arrange：所有真实后端字段明确给出，唯独 API Key 显式设为 None。
    settings = Settings(
        environment="test",
        telemetry_enabled=False,
        persistence_backend="memory",
        llm_backend="openai_compatible",
        llm_model="qwen-plus",
        llm_api_key=None,
        llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    # Act/Assert：模型工厂创建阶段抛固定错误，不包含密钥或服务商响应正文。
    with pytest.raises(ValueError, match="SERVICEOPS_LLM_API_KEY"):
        build_qwen_candidate_evaluation_target(settings)


# 第四条测试验证配置自身逻辑，防止不可能解释的门槛进入付费实验。
def test_candidate_thresholds_reject_worst_rate_above_mean_rate() -> None:
    """最差轮门槛高于平均门槛的反直觉配置必须被拒绝。"""

    # Act/Assert：Pydantic 在构造配置时运行跨字段校验。
    with pytest.raises(ValidationError, match="最差轮通过率门槛不能高于平均通过率门槛"):
        CandidatePromotionThresholds(
            min_mean_overall_pass_rate=0.8,
            min_worst_trial_overall_pass_rate=0.9,
        )
