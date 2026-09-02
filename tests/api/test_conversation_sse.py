"""多轮会话 SSE 工作流事件测试。"""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from serviceops_agent.api.app import app
from serviceops_agent.config.settings import get_settings
from serviceops_agent.security.jwt_auth import create_access_token
from serviceops_agent.security.models import Role


def _headers(subject: str = "user-001") -> dict[str, str]:
    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={Role.CUSTOMER},
    )
    return {"Authorization": f"Bearer {token}"}


def _parse_events(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(
            line.split(": ", maxsplit=1)
            for line in block.splitlines()
            if ": " in line
        )
        events.append((fields["event"], json.loads(fields["data"])))
    return events


@pytest.mark.asyncio
async def test_conversation_stream_returns_accepted_and_typed_result_events() -> None:
    headers = _headers()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        async with client.stream(
            "POST",
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            headers=headers,
            json={
                "message": "发票税号填错了怎么办？",
                "idempotency_key": "sse-turn-0001",
            },
        ) as response:
            body = (await response.aread()).decode()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = _parse_events(body)
    assert events[0] == (
        "accepted",
        {"conversation_id": conversation_id, "phase": "accepted"},
    )
    assert events[-1][0] == "result"
    result = events[-1][1]
    assert result["conversation_id"] == conversation_id
    assert result["answer"]
    assert result["execution_status"] == "completed"


@pytest.mark.asyncio
async def test_conversation_stream_converts_owned_workflow_http_error_to_event() -> None:
    headers = _headers()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        payload = {
            "message": "杭州天气怎么样？",
            "idempotency_key": "sse-conflict-0001",
        }
        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            headers=headers,
            json=payload,
        )
        conflict = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            headers=headers,
            json={**payload, "message": "这是另一条消息"},
        )

    assert first.status_code == 200
    events = _parse_events(conflict.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["status_code"] == 409
    assert events[-1][1]["retryable"] is True
