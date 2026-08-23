"""验证第32步混淆矩阵、安全指标、阈值门和holdout隔离。"""

import asyncio
from pathlib import Path

import pytest

from serviceops_agent.config.paths import PROJECT_ROOT
from serviceops_agent.domain.classification import IntentClassification
from serviceops_agent.domain.enums import Intent
from serviceops_agent.evaluation.intent_classification_experiment import (
    IntentEvaluationCase,
    evaluate_intent_predictions,
    load_intent_experiment_config,
    run_intent_classification_experiment,
)

# 测试与PyCharm脚本共用同一版本化配置。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/intent_classification_experiment.json"


class _AlwaysMedicalFAQClient:
    """模拟把医疗碰撞题高置信错分FAQ的模型，验证前置安全规则能够拦截。"""

    async def classify(self, message: str) -> IntentClassification:
        """所有输入都返回同一个高置信FAQ结果。"""

        del message
        return IntentClassification(intent=Intent.FAQ, confidence=0.95, reason="模拟错分")


def test_confusion_metrics_count_unsafe_auto_route_and_false_return() -> None:
    """人工题误自动化和普通题误入退货写路线必须分别计数。"""

    cases = [
        IntentEvaluationCase(
            case_id="human-to-faq",
            message="天气",
            expected_intent=Intent.HUMAN_HANDOFF,
            risk_level="high",
        ),
        IntentEvaluationCase(
            case_id="faq-to-return",
            message="退货政策",
            expected_intent=Intent.FAQ,
        ),
    ]
    predictions = [
        IntentClassification(intent=Intent.FAQ, confidence=0.9, reason="错误放行"),
        IntentClassification(
            intent=Intent.RETURN_REQUEST,
            confidence=0.9,
            reason="错误写路由",
        ),
    ]
    result = evaluate_intent_predictions(
        cases,
        predictions,
        profile_id="test",
        dataset="development",
        classifier_kind="qwen_candidate",
        confidence_threshold=0.65,
        gate=None,
    )

    assert result.overall_accuracy == 0.0
    assert result.unsafe_auto_route_rate == 1.0
    assert result.false_return_route_rate == 0.5
    assert result.confusion_matrix["human_handoff"]["faq"] == 1


def test_confidence_threshold_routes_low_confidence_prediction_to_human() -> None:
    """低置信度自动意图应由系统覆盖为人工，而不是相信模型标签。"""

    case = IntentEvaluationCase(
        case_id="low-confidence",
        message="不确定问题",
        expected_intent=Intent.HUMAN_HANDOFF,
    )
    prediction = IntentClassification(intent=Intent.FAQ, confidence=0.4, reason="不确定")
    result = evaluate_intent_predictions(
        [case],
        [prediction],
        profile_id="threshold-test",
        dataset="development",
        classifier_kind="qwen_candidate",
        confidence_threshold=0.65,
        gate=None,
    )

    assert result.results[0].predicted_intent == Intent.HUMAN_HANDOFF
    assert result.results[0].passed is True


def test_offline_run_never_reads_or_runs_holdout() -> None:
    """默认实验只产生关键词开发基线，不调用模型也不生成锁定结果。"""

    config = load_intent_experiment_config(CONFIG_PATH)
    report = asyncio.run(run_intent_classification_experiment(config))

    assert report.keyword_development_baseline.total_cases == 32
    assert report.qwen_development_candidates == []
    assert report.successful_chat_calls == 0
    assert report.qwen_holdout_candidate is None


def test_holdout_requires_real_client_and_frozen_candidate() -> None:
    """没有真实客户端或冻结优胜者时，显式holdout请求也必须停止。"""

    config = load_intent_experiment_config(CONFIG_PATH)
    with pytest.raises(ValueError, match="真实千问客户端"):
        asyncio.run(run_intent_classification_experiment(config, include_holdout=True))


def test_safety_qwen_profile_reuses_predictions_and_blocks_medical_collision() -> None:
    """组合候选应复用模型调用，并把明确医疗域外表达强制转人工。"""

    config = load_intent_experiment_config(CONFIG_PATH)
    report = asyncio.run(
        run_intent_classification_experiment(config, qwen_client=_AlwaysMedicalFAQClient())
    )
    safety_profile = next(
        result
        for result in report.qwen_development_candidates
        if result.profile_id == "safety-qwen-v2-threshold-0.65"
    )
    medical_result = next(
        result
        for result in safety_profile.results
        if result.case_id == "intent-dev-human-medical"
    )

    assert report.successful_chat_calls == 32
    assert len(report.qwen_development_candidates) == 8
    assert medical_result.predicted_intent == Intent.HUMAN_HANDOFF
    assert medical_result.passed is True
