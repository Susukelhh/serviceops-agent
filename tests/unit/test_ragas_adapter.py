"""RAGAS 0.4 文档 ID 指标适配层测试。"""

from pathlib import Path

import pytest

pytest.importorskip("ragas", reason="需要可选 eval 依赖组")

from serviceops_agent.config.settings import Settings
from serviceops_agent.evaluation import (
    Ragas04IdMetricRuntime,
    evaluate_retrieval_with_ragas,
    load_rag_evaluation_cases,
)
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


def test_ragas_runtime_uses_official_single_turn_id_metrics() -> None:
    runtime = Ragas04IdMetricRuntime()

    precision, recall = runtime.score(
        retrieved_context_ids=["doc-a", "doc-x"],
        reference_context_ids=["doc-a"],
    )

    assert runtime.version == "0.4.3"
    assert precision == 0.5
    assert recall == 1.0


def test_ragas_adapter_reports_positive_coverage_and_explicit_negative_exclusion() -> None:
    cases = load_rag_evaluation_cases(Path("data/evaluation/rag_retrieval_cases.json"))
    retriever = build_default_knowledge_retriever(
        Settings(
            embedding_backend="hash",
            qdrant_location=":memory:",
            qdrant_collection="test_ragas_adapter",
            rag_reranker="bm25",
        )
    )

    summary = evaluate_retrieval_with_ragas(retriever, cases, top_k=3)

    assert summary.adapter_version == "ragas-id-retrieval-v1"
    assert summary.total_source_cases == 11
    assert summary.evaluated_positive_cases == 8
    assert summary.excluded_negative_cases == 3
    assert summary.mean_id_context_recall == 1.0
    assert summary.advisory_only is True
    assert len(summary.results) == 8
