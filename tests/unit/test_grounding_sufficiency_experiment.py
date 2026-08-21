"""验证第28步离线失败基线、费用保护和提示冻结指纹。"""

# Path声明共享实验配置路径。
from pathlib import Path

# pytest运行异步实验测试。
import pytest

# PROJECT_ROOT保证测试从任意目录启动都定位同一项目。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings构造无密钥离线环境。
from serviceops_agent.config.settings import Settings

# 第28步加载器、提示指纹和运行器是本测试验证的公共接口。
from serviceops_agent.evaluation import (
    grounding_prompt_sha256,
    load_grounding_sufficiency_experiment_config,
    run_grounding_sufficiency_experiment,
)

# CONFIG_PATH与PyCharm示例脚本使用同一版本化契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/grounding_sufficiency_experiment.json"


@pytest.mark.asyncio
async def test_grounding_offline_baseline_exposes_unsupported_answers_without_api() -> None:
    """Extractive应暴露知识缺口风险，默认路径不能读取Key或调用千问。"""

    # 加载真实开发配置。
    config = load_grounding_sufficiency_experiment_config(CONFIG_PATH)
    # 显式清空Key和Base URL，证明离线路径不依赖个人.env。
    settings = Settings(
        llm_backend="mock",
        llm_api_key=None,
        llm_base_url=None,
        telemetry_enabled=False,
    )
    # 不传confirm_paid_api，只运行Extractive开发基线。
    report = await run_grounding_sufficiency_experiment(
        config,
        runtime_settings=settings,
    )

    # 费用边界必须保持关闭。
    assert report.paid_api_called is False
    assert report.actual_chat_calls == 0
    assert report.qwen_development is None
    # 默认绝不运行或读取锁定结果。
    assert report.extractive_holdout is None
    assert report.qwen_holdout is None
    # 16条开发题均分为8条可回答和8条知识缺口。
    baseline = report.extractive_development
    assert baseline.total_cases == 16
    assert baseline.answerable_cases == 8
    assert baseline.unanswerable_cases == 8
    # Extractive只要有证据就回答，因此知识内可用性满分。
    assert baseline.answerable_recall == 1.0
    # 同一行为使全部知识缺口产生无依据回答。
    assert baseline.abstention_accuracy == 0.0
    assert baseline.unsupported_answer_rate == 1.0
    assert baseline.decision_accuracy == 0.5
    # 候选质量门必须失败，证明实验确实暴露问题。
    assert baseline.quality_gate_passed is False
    assert "unsupported_answer_rate_above_threshold" in baseline.quality_gate_failures
    # 真实开发调用计划等于16题，holdout计划只记录10题数量。
    assert report.planned_development_chat_calls == 16
    assert report.planned_holdout_extra_chat_calls == 10


def test_grounding_prompt_fingerprint_is_stable_and_matches_frozen_candidate() -> None:
    """系统提示变化必须显式改变指纹，锁定集前需匹配开发冻结值。"""

    # 加载当前实验配置。
    config = load_grounding_sufficiency_experiment_config(CONFIG_PATH)
    # 当前提示指纹作为开发候选审计身份。
    assert (
        grounding_prompt_sha256()
        == "1c5a43de5b8f50dc4849911527fc233aa0b6aefa0197697b18382e2b48ccad4d"
    )
    # 真实开发候选通过后，配置必须冻结完全相同的提示指纹。
    assert config.frozen_prompt_sha256 == grounding_prompt_sha256()
