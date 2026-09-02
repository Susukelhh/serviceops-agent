"""Cross-Encoder 候选闭包、TEI 协议和安全失败测试。"""

import json
from datetime import date

import httpx
import pytest

from serviceops_agent.config.settings import Settings
from serviceops_agent.domain.knowledge import KnowledgeChunk, RetrievalHit
from serviceops_agent.rag.reranking import (
    CrossEncoderCandidateReranker,
    CrossEncoderServiceError,
    TEICrossEncoderScoringClient,
)


def _hit(chunk_id: str, content: str, score: float) -> RetrievalHit:
    chunk = KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=f"DOC-{chunk_id}",
        title=f"标题-{chunk_id}",
        content=content,
        source=f"knowledge://{chunk_id}",
        version="1.0",
        effective_date=date(2026, 1, 1),
        chunk_index=0,
        start_index=0,
        end_index=len(content),
    )
    return RetrievalHit(
        chunk=chunk,
        score=score,
        dense_score=score,
        dense_rank=1,
        retrieval_channels=["dense"],
        fusion_method="rrf",
    )


class _FixedScorer:
    def score(self, *, query: str, documents: list[str]) -> list[float]:
        assert query == "如何红冲发票"
        assert len(documents) == 2
        return [0.12, 0.94]


def test_cross_encoder_reorders_only_existing_candidates() -> None:
    first = _hit("logistics", "查看物流进度", 0.91)
    second = _hit("invoice", "发票抬头错误可以红冲重开", 0.78)
    reranker = CrossEncoderCandidateReranker(scoring_client=_FixedScorer())

    ranked = reranker.rerank(
        query="如何红冲发票",
        hits=[first, second],
        top_k=1,
    )

    assert [hit.chunk.chunk_id for hit in ranked] == ["invoice"]
    assert ranked[0].score == 0.94
    assert ranked[0].dense_score == second.dense_score
    assert ranked[0].fusion_method == "cross_encoder"


def test_tei_client_restores_scores_to_input_order_and_uses_bearer_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rerank"
        assert request.headers["authorization"] == "Bearer local-test-key"
        payload = json.loads(request.content)
        assert payload["texts"] == ["文档一", "文档二"]
        return httpx.Response(
            200,
            json=[
                {"index": 1, "score": 0.91},
                {"index": 0, "score": 0.23},
            ],
        )

    client = TEICrossEncoderScoringClient(
        base_url="http://tei.local",
        api_key="local-test-key",
        transport=httpx.MockTransport(handler),
    )

    assert client.score(query="测试", documents=["文档一", "文档二"]) == [0.23, 0.91]


def test_tei_failure_does_not_expose_remote_response_body() -> None:
    secret_body = "provider-secret-debug-payload"

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(503, text=secret_body)

    client = TEICrossEncoderScoringClient(
        base_url="http://tei.local",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CrossEncoderServiceError) as captured:
        client.score(query="测试", documents=["文档"])
    assert secret_body not in str(captured.value)


def test_cross_encoder_settings_require_complete_candidate_pool() -> None:
    with pytest.raises(ValueError, match="两路召回数"):
        Settings(
            rag_reranker="cross_encoder",
            rag_cross_encoder_url="http://tei.local",
            rag_rerank_candidate_k=5,
            rag_hybrid_dense_k=4,
            rag_hybrid_lexical_k=5,
        )

