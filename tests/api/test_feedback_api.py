"""用户反馈、问题池审核和知识候选导出API测试。"""

import pytest
from httpx import ASGITransport, AsyncClient

from serviceops_agent.api.app import app
from serviceops_agent.config.settings import get_settings
from serviceops_agent.security.jwt_auth import create_access_token
from serviceops_agent.security.models import Role


def _headers(subject: str, role: Role) -> dict[str, str]:
    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={role},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_feedback_owner_boundary_review_and_candidate_export() -> None:
    owner = _headers("user-001", Role.CUSTOMER)
    other = _headers("user-002", Role.CUSTOMER)
    curator = _headers("curator-001", Role.KNOWLEDGE_CURATOR)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner)
        conversation_id = created.json()["conversation_id"]
        message = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            headers=owner,
            json={
                "message": "发票税号填错了怎么办？",
                "idempotency_key": "feedback-turn-0001",
            },
        )
        assert message.status_code == 200
        turn_id = message.json()["turn_id"]
        feedback_path = (
            f"/api/v1/conversations/{conversation_id}/turns/{turn_id}/feedback"
        )
        payload = {
            "idempotency_key": "feedback-http-0001",
            "signal": "unhelpful",
            "reason": "missing_information",
        }

        hidden = await client.post(feedback_path, headers=other, json=payload)
        first = await client.post(feedback_path, headers=owner, json=payload)
        replay = await client.post(feedback_path, headers=owner, json=payload)
        forbidden_queue = await client.get("/api/v1/internal/feedback", headers=owner)
        queue = await client.get("/api/v1/internal/feedback", headers=curator)

        assert hidden.status_code == 404
        assert first.status_code == 201
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert forbidden_queue.status_code == 403
        assert queue.status_code == 200
        assert len(queue.json()["items"]) == 1
        feedback_id = first.json()["feedback_id"]

        reviewed = await client.post(
            f"/api/v1/internal/feedback/{feedback_id}/review",
            headers=curator,
            json={
                "category": "knowledge_gap",
                "proposed_title": "电子发票红冲所需信息",
                "proposed_answer": "申请红冲重开时需要提供原发票号码和正确的企业抬头信息。",
            },
        )
        candidates = await client.get(
            "/api/v1/internal/feedback/knowledge-candidates",
            headers=curator,
        )

    assert reviewed.status_code == 200
    assert reviewed.json()["items"][0]["status"] == "knowledge_candidate"
    assert candidates.status_code == 200
    assert candidates.json()["candidates"][0]["source_feedback_id"] == feedback_id


@pytest.mark.asyncio
async def test_human_handoff_is_automatically_added_to_feedback_queue() -> None:
    owner = _headers("user-001", Role.CUSTOMER)
    curator = _headers("curator-001", Role.KNOWLEDGE_CURATOR)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner)
        response = await client.post(
            f"/api/v1/conversations/{created.json()['conversation_id']}/messages",
            headers=owner,
            json={"message": "杭州天气怎么样？", "idempotency_key": "handoff-turn-0001"},
        )
        queue = await client.get("/api/v1/internal/feedback", headers=curator)

    assert response.status_code == 200
    assert response.json()["requires_human"] is True
    assert queue.status_code == 200
    assert queue.json()["items"][0]["signal"] == "auto_handoff"
