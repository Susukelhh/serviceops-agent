"""验证第29步端到端RAG离线基线、分层归因、费用保护和候选冻结。"""

# Path声明共享实验配置路径。
from pathlib import Path

# pytest支持异步评测函数。
import pytest

# PROJECT_ROOT保证测试从任意目录启动都能定位同一配置。
from serviceops_agent.config.paths import PROJECT_ROOT

# Settings构造完全无Key的离线环境。
from serviceops_agent.config.settings import Settings

# GroundedAnswerDraft与RetrievalHit用于创建可控测试替身。
from serviceops_agent.domain.knowledge import GroundedAnswerDraft, RetrievalHit

# 第29步公共接口是本文件验证对象。
from serviceops_agent.evaluation import (
    RAGEndToEndCase,
    RAGEndToEndQualityGate,
    evaluate_rag_end_to_end_pipeline,
    load_rag_end_to_end_experiment_config,
    rag_end_to_end_candidate_fingerprint,
    run_rag_end_to_end_experiment,
)

# 确定性范围门测试天气拦截。
from serviceops_agent.rag.query_policy import DeterministicFAQScopePolicy

# KnowledgeRetriever协议的测试替身只需实现search。
from serviceops_agent.rag.retriever import KnowledgeRetriever

# CONFIG_PATH与第29步PyCharm示例使用同一版本化契约。
CONFIG_PATH: Path = PROJECT_ROOT / "data/evaluation/rag_end_to_end_experiment.json"


@pytest.mark.asyncio
async def test_end_to_end_offline_baseline_never_calls_paid_api_or_holdout() -> None:
    """默认运行只能建立Hash基线，不能读取密钥、千问候选或锁定结果。"""

    # 读取真实实验配置。
    config = load_rag_end_to_end_experiment_config(CONFIG_PATH)
    # 清空所有真实模型配置证明离线路径独立。
    settings = Settings(
        llm_backend="mock",
        llm_api_key=None,
        llm_base_url=None,
        telemetry_enabled=False,
    )
    # 不提供任何付费确认。
    report = await run_rag_end_to_end_experiment(
        config,
        runtime_settings=settings,
    )

    # 所有真实调用计数必须为零。
    assert report.paid_api_called is False
    assert report.actual_embedding_requests == 0
    assert report.actual_embedding_input_tokens == 0
    assert report.actual_chat_calls == 0
    # 候选和holdout必须保持未运行。
    assert report.candidate_development is None
    assert report.baseline_holdout is None
    assert report.candidate_holdout is None
    # 开发集固定16条，包含11条可回答和5条不可回答。
    assert report.baseline_development.total_cases == 16
    assert report.baseline_development.answerable_cases == 11
    assert report.baseline_development.unanswerable_cases == 5
    # 旧Extractive对允许进入检索且命中证据的知识缺口会产生风险。
    assert report.baseline_development.unsupported_answer_rate > 0.0


def test_end_to_end_candidate_fingerprint_is_stable_and_matches_frozen_profile() -> None:
    """真实开发通过后，完整候选指纹必须稳定且与冻结配置完全一致。"""

    # 加载同一JSON两次模拟不同进程。
    first_config = load_rag_end_to_end_experiment_config(CONFIG_PATH)
    second_config = load_rag_end_to_end_experiment_config(CONFIG_PATH)
    # 相同版本化参数必须得到相同SHA-256。
    assert rag_end_to_end_candidate_fingerprint(
        first_config
    ) == rag_end_to_end_candidate_fingerprint(second_config)
    # 真实开发质量门已经通过，配置只冻结脚本打印的同一64位指纹。
    assert first_config.frozen_candidate_fingerprint == (
        "197c37b7c7e888a685eb5e4d1a79b99b648da11f04311cdbe1a52bd61f649f47"
    )
    # 冻结值必须等于从当前全部候选参数重新计算出的指纹。
    assert first_config.frozen_candidate_fingerprint == rag_end_to_end_candidate_fingerprint(
        first_config
    )


class _AlwaysEmptyRetriever:
    """永远返回空证据，用于验证检索失败归因。"""

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """忽略问题和K值并返回空列表。"""

        # 两个参数只用于满足线上KnowledgeRetriever协议。
        del query, top_k
        # 空列表模拟阈值后没有任何证据。
        return []


class _NeverCalledAnswerClient:
    """若被调用就主动失败，证明范围/空检索会跳过聊天模型。"""

    async def generate(
        self,
        *,
        question: str,
        evidence: list[RetrievalHit],
    ) -> GroundedAnswerDraft:
        """该路径理论上不可到达。"""

        # 显式引用参数满足协议并避免静态检查警告。
        del question, evidence
        # 若实验器错误调用回答器，测试立即失败。
        raise AssertionError("没有证据时不应调用回答模型")


@pytest.mark.asyncio
async def test_end_to_end_failure_attribution_separates_scope_and_retrieval() -> None:
    """正例应分别归因为范围误拒和检索缺失，而不是笼统写回答错误。"""

    # 第一题被天气规则拒绝，第二题通过范围门但检索为空。
    cases = [
        RAGEndToEndCase(
            case_id="scope-failure",
            question="杭州天气如何，我的普通商品还能退吗？",
            expected_document_ids=["KB-RETURN-GENERAL-001"],
            should_answer=True,
        ),
        RAGEndToEndCase(
            case_id="retrieval-failure",
            question="普通商品签收四天还能退吗？",
            expected_document_ids=["KB-RETURN-GENERAL-001"],
            should_answer=True,
        ),
        RAGEndToEndCase(
            case_id="safe-negative",
            question="杭州明天会下雨吗？",
            expected_document_ids=[],
            should_answer=False,
        ),
    ]
    # 宽松门只为本测试关注逐题归因，不影响生产配置。
    gate = RAGEndToEndQualityGate(
        min_retrieval_recall=0.0,
        min_top_1_accuracy=0.0,
        min_answerable_recall=0.0,
        min_abstention_accuracy=0.0,
        min_decision_accuracy=0.0,
        max_unsupported_answer_rate=1.0,
        min_citation_validity=0.0,
    )
    # 类型注解证明替身遵循最小检索协议。
    retriever: KnowledgeRetriever = _AlwaysEmptyRetriever()
    # 执行完整公开决策链。
    summary = await evaluate_rag_end_to_end_pipeline(
        profile_id="attribution-test",
        dataset="development",
        cases=cases,
        query_policy=DeterministicFAQScopePolicy(),
        retriever=retriever,
        answer_client=_NeverCalledAnswerClient(),
        top_k=5,
        gate=gate,
    )

    # 按稳定ID取得结果。
    by_id = {result.case_id: result for result in summary.results}
    # 天气混合问题停在范围门并明确标记误拒。
    assert by_id["scope-failure"].terminal_stage == "scope_rejected"
    assert "scope_false_rejection" in by_id["scope-failure"].failure_codes
    # 普通售后问题通过范围门，失败应定位到检索层。
    assert by_id["retrieval-failure"].terminal_stage == "retrieval_empty"
    assert "retrieval_miss" in by_id["retrieval-failure"].failure_codes
    # 两题都没有证据，因此聊天调用必须为零。
    assert summary.grounding_chat_calls == 0
