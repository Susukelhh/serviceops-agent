"""多轮会话HTTP API的所有权、幂等重放和并发边界测试。"""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from serviceops_agent.api.app import app
from serviceops_agent.config.settings import get_settings
from serviceops_agent.domain.conversation import (
    ConversationExecutionLease,
    ConversationExecutionRecoveryResult,
    ConversationTurnStatus,
    ConversationTurnUpdate,
    ExecutionKind,
    ExecutionLeaseState,
)
from serviceops_agent.infrastructure.conversation_repository import ConversationRepository
from serviceops_agent.infrastructure.runtime import CheckpointDeleter
from serviceops_agent.security.jwt_auth import create_access_token
from serviceops_agent.security.models import Role

api_app_module = importlib.import_module("serviceops_agent.api.app")


def _customer_headers(subject: str) -> dict[str, str]:
    """为一个普通客户签发具有agent:chat Scope的测试Token。"""

    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={Role.CUSTOMER},
    )
    return {"Authorization": f"Bearer {token}"}


def _reviewer_headers(subject: str) -> dict[str, str]:
    """为独立审批人签发return:approve测试Token。"""

    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={Role.RETURN_REVIEWER},
    )
    return {"Authorization": f"Bearer {token}"}


def _operator_headers(subject: str) -> dict[str, str]:
    """为会话到期清理签发独立operator Scope测试Token。"""

    token = create_access_token(
        settings=get_settings(),
        subject=subject,
        roles={Role.OPERATOR},
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_conversation_create_message_replay_sequence_and_owner_isolation() -> None:
    """同键重试不能重复执行，不同用户不能枚举或使用该会话。"""

    owner_headers = _customer_headers("user-001")
    other_headers = _customer_headers("user-002")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post(
            "/api/v1/conversations",
            headers=owner_headers,
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]
        assert created.json()["memory_version"] == 0

        first_request = {
            "message": "查询订单 SO100001",
            "idempotency_key": "turn-order-0001",
        }
        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=first_request,
            headers=owner_headers,
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["conversation_id"] == conversation_id
        assert first_payload["sequence_number"] == 1
        assert first_payload["replayed"] is False
        assert first_payload["execution_status"] == "completed"
        repository: ConversationRepository = app.state.conversation_repository
        first_turn = repository.get_turn_by_workflow_thread(
            workflow_thread_id=UUID(first_payload["thread_id"]),
        )
        assert first_turn is not None
        first_lease = repository.get_turn_execution_lease(turn_id=first_turn.turn_id)
        assert first_lease is not None
        assert first_lease.kind == ExecutionKind.INITIAL
        assert first_lease.state == ExecutionLeaseState.RELEASED
        assert first_lease.fence_generation == 1
        assert "claim_token" not in first.text.lower()
        assert "claim token" not in first.text.lower()

        replay = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=first_request,
            headers=owner_headers,
        )
        assert replay.status_code == 200
        replay_payload = replay.json()
        assert replay_payload["replayed"] is True
        assert replay_payload["turn_id"] == first_payload["turn_id"]
        assert replay_payload["thread_id"] == first_payload["thread_id"]
        assert replay_payload["answer"] == first_payload["answer"]

        key_conflict = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "同一个键却换成另一条消息",
                "idempotency_key": "turn-order-0001",
            },
            headers=owner_headers,
        )
        assert key_conflict.status_code == 409
        assert key_conflict.json() == {"detail": "幂等键已用于另一条消息"}

        second = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "查询订单 SO100002",
                "idempotency_key": "turn-order-0002",
            },
            headers=owner_headers,
        )
        assert second.status_code == 200
        assert second.json()["sequence_number"] == 2
        assert second.json()["thread_id"] != first_payload["thread_id"]

        detail = await client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        assert detail.status_code == 200
        assert [turn["sequence_number"] for turn in detail.json()["turns"]] == [1, 2]

        hidden_get = await client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=other_headers,
        )
        hidden_send = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "尝试访问别人的会话",
                "idempotency_key": "other-user-0001",
            },
            headers=other_headers,
        )
        assert hidden_get.status_code == 404
        assert hidden_send.status_code == 404
        assert hidden_get.json() == hidden_send.json() == {"detail": "未找到可用会话"}


@pytest.mark.asyncio
async def test_conversation_running_turn_returns_conflict_instead_of_duplicate_execution() -> None:
    """同一轮已被请求接管时，第二个HTTP请求必须快速冲突而不是重跑图。"""

    owner = "user-001"
    headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = UUID(created.json()["conversation_id"])
        repository: ConversationRepository = app.state.conversation_repository
        turn, replayed = repository.create_or_get_turn(
            conversation_id=conversation_id,
            owner_user_id=owner,
            idempotency_key="running-turn-0001",
            user_message="查询订单 SO100001",
        )
        assert replayed is False
        repository.advance_turn(
            conversation_id=conversation_id,
            turn_id=turn.turn_id,
            owner_user_id=owner,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.ACCEPTED,
                status=ConversationTurnStatus.RUNNING,
            ),
        )

        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "查询订单 SO100001",
                "idempotency_key": "running-turn-0001",
            },
            headers=headers,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "该轮消息仍在处理中"}


@pytest.mark.asyncio
async def test_conversation_replay_does_not_duplicate_shadow_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """影子指标只记录首次真实执行；相同幂等键重放不能污染窗口。"""

    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        api_app_module.settings,
        "conversation_shadow_enabled",
        True,
    )
    monkeypatch.setattr(
        api_app_module.settings,
        "conversation_shadow_sample_rate",
        1.0,
    )
    monkeypatch.setattr(
        api_app_module,
        "record_conversation_shadow_observation",
        lambda **attributes: recorded.append(attributes),
    )

    owner_headers = _customer_headers("shadow-replay-owner")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post(
            "/api/v1/conversations",
            headers=owner_headers,
        )
        conversation_id = created.json()["conversation_id"]
        request = {
            "message": "查询订单 SO100001",
            "idempotency_key": "shadow-replay-0001",
        }
        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=request,
            headers=owner_headers,
        )
        replay = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=request,
            headers=owner_headers,
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.json()["replayed"] is True
    assert len(recorded) == 1
    assert set(recorded[0]) == {
        "candidate_id",
        "intent",
        "outcome",
        "resolution_reason",
        "model_failure",
        "evidence_abstention",
        "ambiguous_context",
        "human_handoff",
        "safety_violation_codes",
    }
    assert recorded[0]["candidate_id"] == "local-baseline"


@pytest.mark.asyncio
async def test_conversation_message_requires_explicit_idempotency_key() -> None:
    """多轮消息入口不允许省略幂等键或偷偷提交身份字段。"""

    headers = _customer_headers("user-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        response = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"message": "查询订单 SO100001", "user_id": "attacker"},
            headers=headers,
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_order_follow_up_uses_verified_previous_order_without_resending_id() -> None:
    """第二轮“它”应绑定第一轮真正查过的订单，并保存可审计独立问题。"""

    headers = _customer_headers("user-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "查询订单 SO100001",
                "idempotency_key": "follow-up-first-0001",
            },
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["queried_order_ids"] == ["SO100001"]

        follow_up = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "它现在到哪了？",
                "idempotency_key": "follow-up-second-0002",
            },
            headers=headers,
        )
        assert follow_up.status_code == 200
        assert follow_up.json()["queried_order_ids"] == ["SO100001"]
        assert "conversation:resolution_verified_order_reference" in follow_up.json()[
            "events"
        ]

        detail = await client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
        )

    assert detail.status_code == 200
    second_turn = detail.json()["turns"][1]
    assert second_turn["user_message"] == "它现在到哪了？"
    assert second_turn["standalone_question"] == "关于订单 SO100001，它现在到哪了？"


@pytest.mark.asyncio
async def test_ambiguous_follow_up_after_multi_order_query_asks_for_order_id() -> None:
    """上一轮包含多个订单时，“这个订单”不能被系统擅自绑定。"""

    headers = _customer_headers("user-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        first = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "查询订单 SO100001 和 SO100002",
                "idempotency_key": "ambiguous-first-0001",
            },
            headers=headers,
        )
        assert first.status_code == 200
        assert first.json()["queried_order_ids"] == ["SO100001", "SO100002"]

        follow_up = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "这个订单什么时候到？",
                "idempotency_key": "ambiguous-second-0002",
            },
            headers=headers,
        )

    assert follow_up.status_code == 200
    assert follow_up.json()["needs_clarification"] is True
    assert follow_up.json()["queried_order_ids"] == []
    assert "conversation:resolution_ambiguous_order_reference" in follow_up.json()[
        "events"
    ]


@pytest.mark.asyncio
async def test_memory_accepts_only_owned_order_and_grounded_citation() -> None:
    """查询过但不可用的订单不能进记忆，grounded FAQ引用可以进入来源槽位。"""

    owner = "user-001"
    headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = UUID(created.json()["conversation_id"])
        unavailable = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "查询订单 SO200001",
                "idempotency_key": "memory-unowned-0001",
            },
            headers=headers,
        )
        assert unavailable.status_code == 200
        assert unavailable.json()["queried_order_ids"] == ["SO200001"]

        faq = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "发票税号写错了怎么办",
                "idempotency_key": "memory-faq-0002",
            },
            headers=headers,
        )
        assert faq.status_code == 200
        assert faq.json()["citations"][0]["document_id"] == "KB-INVOICE-001"
        faq_document_ids = [
            citation["document_id"] for citation in faq.json()["citations"]
        ]
        repository: ConversationRepository = app.state.conversation_repository
        conversation = repository.get_conversation_for_owner(
            conversation_id=conversation_id,
            owner_user_id=owner,
        )

    assert conversation is not None
    assert conversation.memory.active_order_id is None
    assert conversation.memory.recent_order_ids == []
    assert conversation.memory.recent_document_ids == faq_document_ids
    assert conversation.memory.last_intent == "faq"
    assert conversation.memory.last_processed_sequence == 2


@pytest.mark.asyncio
async def test_approval_resume_immediately_completes_conversation_turn_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会话内退货审批恢复后，无需消息重放也应直接同步轮次和活动订单。"""

    owner = "user-001"
    owner_headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner_headers)
        conversation_id = UUID(created.json()["conversation_id"])
        initial = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "为订单 SO100002 申请退货，原因：商品尺寸确实不合适",
                "idempotency_key": "conversation-approval-0001",
            },
            headers=owner_headers,
        )
        assert initial.status_code == 200
        assert initial.json()["execution_status"] == "approval_required"
        thread_id = initial.json()["thread_id"]
        repository: ConversationRepository = app.state.conversation_repository
        initial_turn = repository.get_turn_by_workflow_thread(
            workflow_thread_id=UUID(thread_id),
        )
        assert initial_turn is not None
        original_claim = repository.claim_turn_execution
        approval_claim_statuses: list[ConversationTurnStatus] = []

        def record_approval_claim(
            *,
            conversation_id: UUID,
            turn_id: UUID,
            owner_user_id: str,
            execution_kind: ExecutionKind,
            lease_seconds: int,
            decision_audit_event_id: str | None = None,
        ) -> ConversationExecutionLease:
            lease = original_claim(
                conversation_id=conversation_id,
                turn_id=turn_id,
                owner_user_id=owner_user_id,
                execution_kind=execution_kind,
                lease_seconds=lease_seconds,
                decision_audit_event_id=decision_audit_event_id,
            )
            if execution_kind == ExecutionKind.APPROVAL_RESUME:
                claimed_turn = repository.get_turn_by_workflow_thread(
                    workflow_thread_id=UUID(thread_id),
                )
                assert claimed_turn is not None
                approval_claim_statuses.append(claimed_turn.status)
            return lease

        monkeypatch.setattr(repository, "claim_turn_execution", record_approval_claim)

        approval = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": True, "comment": "会话同步测试批准"},
            headers=_reviewer_headers("conversation-reviewer-001"),
        )
        assert approval.status_code == 200
        assert approval.json()["return_workflow_status"] == "completed"

        detail = await client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        conversation = repository.get_conversation_for_owner(
            conversation_id=conversation_id,
            owner_user_id=owner,
        )
        latest_lease = repository.get_turn_execution_lease(
            turn_id=initial_turn.turn_id,
        )

    assert detail.status_code == 200
    assert detail.json()["turns"][0]["status"] == "completed"
    assert detail.json()["turns"][0]["assistant_answer"] == approval.json()["answer"]
    assert conversation is not None
    assert conversation.memory.active_order_id == "SO100002"
    assert conversation.memory.recent_order_ids == ["SO100002"]
    assert conversation.memory.last_intent == "return_request"
    assert approval_claim_statuses == [ConversationTurnStatus.WAITING_APPROVAL]
    assert latest_lease is not None
    assert latest_lease.kind == ExecutionKind.APPROVAL_RESUME
    assert latest_lease.state == ExecutionLeaseState.RELEASED
    assert latest_lease.fence_generation == 2
    assert "claim_token" not in approval.text.lower()


@pytest.mark.asyncio
async def test_concurrent_identical_approvals_resume_conversation_workflow_once() -> None:
    """两个相同审批并发提交时，只能有一个请求获得轮次CAS并恢复工作流。"""

    owner_headers = _customer_headers("user-001")
    reviewer_headers = _reviewer_headers("concurrent-reviewer-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner_headers)
        conversation_id = created.json()["conversation_id"]
        initial = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "为订单 SO100002 申请退货，原因：并发审批测试商品不合适",
                "idempotency_key": "concurrent-approval-0001",
            },
            headers=owner_headers,
        )
        thread_id = initial.json()["thread_id"]
        approval_body = {"approved": True, "comment": "完全相同的并发决定"}

        responses = await asyncio.gather(
            client.post(
                f"/api/v1/approvals/{thread_id}",
                json=approval_body,
                headers=reviewer_headers,
            ),
            client.post(
                f"/api/v1/approvals/{thread_id}",
                json=approval_body,
                headers=reviewer_headers,
            ),
        )
        return_count = app.state.return_request_repository.count()

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert return_count == 1


@pytest.mark.asyncio
async def test_uncertain_conversation_approval_error_is_quarantined_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审批图异常不能猜测业务失败；租约到期后必须进入人工对账。"""

    owner = "user-001"
    owner_headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner_headers)
        conversation_id = UUID(created.json()["conversation_id"])
        initial = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "为订单 SO100002 申请退货，原因：商品尺寸确实不合适",
                "idempotency_key": "uncertain-approval-0001",
            },
            headers=owner_headers,
        )
        assert initial.status_code == 200
        thread_id = UUID(initial.json()["thread_id"])
        graph = app.state.service_graph

        async def fail_resume(*_: object, **__: object) -> None:
            raise OSError("simulated uncertain approval execution")

        monkeypatch.setattr(graph, "ainvoke", fail_resume)
        approval = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": True, "comment": "异常隔离测试"},
            headers=_reviewer_headers("uncertain-reviewer-001"),
        )

        repository: ConversationRepository = app.state.conversation_repository
        turn = repository.get_turn_by_workflow_thread(workflow_thread_id=thread_id)
        assert turn is not None
        active_lease = repository.get_turn_execution_lease(turn_id=turn.turn_id)
        assert active_lease is not None
        audit_events = app.state.approval_audit_repository.list_for_thread(
            str(thread_id)
        )
        recovery = repository.recover_stale_turn_executions(
            now=active_lease.lease_expires_at + timedelta(seconds=1),
            grace_seconds=0,
            accepted_stale_seconds=60,
        )
        quarantined = repository.get_turn_execution_lease(turn_id=turn.turn_id)

    assert approval.status_code == 500
    assert turn.status == ConversationTurnStatus.WAITING_APPROVAL
    assert active_lease.state == ExecutionLeaseState.ACTIVE
    assert [event.event_type.value for event in audit_events] == [
        "approval_decision_recorded"
    ]
    assert recovery.approval_quarantined_count == 1
    assert quarantined is not None
    assert quarantined.state == ExecutionLeaseState.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_owner_delete_removes_checkpoints_and_is_not_enumerable() -> None:
    """越权、随机和重复删除统一204；所有者删除同时清除Checkpoint与映射。"""

    owner = "delete-owner-001"
    owner_headers = _customer_headers(owner)
    other_headers = _customer_headers("delete-other-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner_headers)
        conversation_id = created.json()["conversation_id"]
        message = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "发票税号写错了怎么办",
                "idempotency_key": "delete-checkpoint-0001",
            },
            headers=owner_headers,
        )
        assert message.status_code == 200
        thread_id = message.json()["thread_id"]
        graph_config = {"configurable": {"thread_id": thread_id}}
        assert (await app.state.service_graph.aget_state(graph_config)).values

        hidden = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=other_headers,
        )
        random_missing = await client.delete(
            f"/api/v1/conversations/{uuid4()}",
            headers=other_headers,
        )
        still_owned = await client.get(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        deleted = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        repeated = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=owner_headers,
        )
        repository: ConversationRepository = app.state.conversation_repository

        assert hidden.status_code == random_missing.status_code == 204
        assert still_owned.status_code == 200
        assert deleted.status_code == repeated.status_code == 204
        assert repository.count_conversations() == 0
        assert repository.get_turn_by_workflow_thread(
            workflow_thread_id=UUID(thread_id)
        ) is None
        assert not (await app.state.service_graph.aget_state(graph_config)).values


@pytest.mark.asyncio
async def test_two_concurrent_owner_deletes_are_both_idempotent_successes() -> None:
    """两个请求持有同一关闭计划时，后完成者不能把已删除误报成503。"""

    class BarrierCheckpointDeleter:
        def __init__(self, delegate: CheckpointDeleter) -> None:
            self._delegate = delegate
            self._arrived = 0
            self._both_arrived = asyncio.Event()

        async def adelete_thread(self, thread_id: str) -> None:
            self._arrived += 1
            if self._arrived == 2:
                self._both_arrived.set()
            await self._both_arrived.wait()
            await self._delegate.adelete_thread(thread_id)

    owner = "delete-concurrent-owner-001"
    headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = created.json()["conversation_id"]
        message = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "发票怎么开？",
                "idempotency_key": "delete-concurrent-turn-0001",
            },
            headers=headers,
        )
        assert message.status_code == 200

        real_deleter = app.state.checkpoint_deleter
        app.state.checkpoint_deleter = BarrierCheckpointDeleter(real_deleter)
        try:
            responses = await asyncio.gather(
                client.delete(
                    f"/api/v1/conversations/{conversation_id}",
                    headers=headers,
                ),
                client.delete(
                    f"/api/v1/conversations/{conversation_id}",
                    headers=headers,
                ),
            )
        finally:
            app.state.checkpoint_deleter = real_deleter

        repository: ConversationRepository = app.state.conversation_repository
        assert [response.status_code for response in responses] == [204, 204]
        assert repository.count_conversations() == 0


@pytest.mark.asyncio
async def test_owner_delete_rejects_active_turn() -> None:
    """仍可能写回结果的accepted/running轮次必须阻止删除会话。"""

    owner = "delete-busy-owner-001"
    headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = UUID(created.json()["conversation_id"])
        repository: ConversationRepository = app.state.conversation_repository
        repository.create_or_get_turn(
            conversation_id=conversation_id,
            owner_user_id=owner,
            idempotency_key="delete-busy-turn-0001",
            user_message="这条消息仍待执行",
        )

        response = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
        )
        record = repository.get_conversation_for_owner(
            conversation_id=conversation_id,
            owner_user_id=owner,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "会话仍有消息正在处理中"}
    assert record is not None
    assert record.status.value == "active"


@pytest.mark.asyncio
async def test_checkpoint_delete_failure_keeps_closed_mapping_for_retry() -> None:
    """Saver故障返回503且不先删业务清单，恢复后同一DELETE可以安全完成。"""

    class FailingCheckpointDeleter:
        async def adelete_thread(self, thread_id: str) -> None:
            _ = thread_id
            raise OSError("simulated checkpoint failure")

    owner = "delete-retry-owner-001"
    headers = _customer_headers(owner)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=headers)
        conversation_id = UUID(created.json()["conversation_id"])
        message = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "message": "发票税号写错了怎么办",
                "idempotency_key": "delete-retry-turn-0001",
            },
            headers=headers,
        )
        thread_id = UUID(message.json()["thread_id"])
        real_checkpoint_deleter = app.state.checkpoint_deleter
        app.state.checkpoint_deleter = FailingCheckpointDeleter()
        failed = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
        )
        repository: ConversationRepository = app.state.conversation_repository
        preserved = repository.get_conversation_for_owner(
            conversation_id=conversation_id,
            owner_user_id=owner,
        )
        preserved_turn = repository.get_turn_by_workflow_thread(
            workflow_thread_id=thread_id
        )

        app.state.checkpoint_deleter = real_checkpoint_deleter
        retried = await client.delete(
            f"/api/v1/conversations/{conversation_id}",
            headers=headers,
        )

    assert failed.status_code == 503
    assert failed.headers["Retry-After"] == "1"
    assert preserved is not None
    assert preserved.status.value == "closed"
    assert preserved_turn is not None
    assert retried.status_code == 204
    assert repository.count_conversations() == 0


@pytest.mark.asyncio
async def test_delete_prepare_failure_returns_low_sensitivity_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """业务仓库故障不能把驱动异常或目标会话存在性泄漏给DELETE调用者。"""

    def fail_prepare(**_: object) -> None:
        raise OSError("simulated repository address and target details")

    headers = _customer_headers("delete-prepare-failure-owner-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        repository: ConversationRepository = app.state.conversation_repository
        monkeypatch.setattr(repository, "prepare_conversation_deletion", fail_prepare)
        response = await client.delete(
            f"/api/v1/conversations/{uuid4()}",
            headers=headers,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "会话清理暂未完成，请稍后重试"}
    assert response.headers["Retry-After"] == "1"
    assert "repository" not in response.text


@pytest.mark.asyncio
async def test_operator_cleanup_requires_scope_and_only_deletes_due_conversations() -> None:
    """普通用户不能触发生命周期任务；operator只得到低敏聚合计数。"""

    customer_headers = _customer_headers("cleanup-customer-001")
    operator_headers = _operator_headers("cleanup-operator-001")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        repository: ConversationRepository = app.state.conversation_repository
        due = repository.create_conversation(
            owner_user_id="cleanup-due-owner-001",
            expires_at=datetime.now(UTC) + timedelta(milliseconds=200),
        )
        future = repository.create_conversation(
            owner_user_id="cleanup-future-owner-001",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        forbidden = await client.post(
            "/api/v1/internal/conversations/cleanup",
            headers=customer_headers,
        )
        await asyncio.sleep(0.25)
        cleaned = await client.post(
            "/api/v1/internal/conversations/cleanup?limit=10",
            headers=operator_headers,
        )

        assert forbidden.status_code == 403
        assert cleaned.status_code == 200
        assert cleaned.json() == {
            "scanned_count": 1,
            "deleted_count": 1,
            "failed_count": 0,
        }
        assert repository.get_conversation_for_owner(
            conversation_id=due.conversation_id,
            owner_user_id=due.owner_user_id,
        ) is None
        assert repository.get_conversation_for_owner(
            conversation_id=future.conversation_id,
            owner_user_id=future.owner_user_id,
        ) is not None


@pytest.mark.asyncio
async def test_workflow_recovery_requires_operator_and_returns_only_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复扫描必须分权，成功响应只能包含五项低敏聚合计数。"""

    result = ConversationExecutionRecoveryResult(
        scanned_count=9,
        accepted_failed_count=2,
        initial_failed_count=3,
        approval_quarantined_count=1,
        legacy_manual_review_count=2,
    )

    def fixed_recovery(**_: object) -> ConversationExecutionRecoveryResult:
        return result

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        repository: ConversationRepository = app.state.conversation_repository
        monkeypatch.setattr(
            repository,
            "recover_stale_turn_executions",
            fixed_recovery,
        )
        forbidden = await client.post(
            "/api/v1/internal/conversations/recover-stale",
            headers=_customer_headers("recovery-customer-001"),
        )
        recovered = await client.post(
            "/api/v1/internal/conversations/recover-stale?limit=25",
            headers=_operator_headers("recovery-operator-001"),
        )

    assert forbidden.status_code == 403
    assert recovered.status_code == 200
    assert recovered.json() == {
        "scanned_count": 9,
        "accepted_failed_count": 2,
        "initial_failed_count": 3,
        "approval_quarantined_count": 1,
        "legacy_manual_review_count": 2,
    }
    assert set(recovered.json()) == {
        "scanned_count",
        "accepted_failed_count",
        "initial_failed_count",
        "approval_quarantined_count",
        "legacy_manual_review_count",
    }


@pytest.mark.asyncio
async def test_workflow_recovery_failure_returns_fixed_low_sensitivity_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓库驱动异常只能映射为固定503，不能回显连接或目标轮次信息。"""

    def fail_recovery(**_: object) -> ConversationExecutionRecoveryResult:
        raise OSError("postgresql://admin:secret@internal/recovery turn-private-001")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        repository: ConversationRepository = app.state.conversation_repository
        monkeypatch.setattr(
            repository,
            "recover_stale_turn_executions",
            fail_recovery,
        )
        response = await client.post(
            "/api/v1/internal/conversations/recover-stale",
            headers=_operator_headers("recovery-failure-operator-001"),
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "工作流恢复服务暂不可用"}
    assert "postgresql" not in response.text
    assert "admin:secret" not in response.text
    assert "turn-private-001" not in response.text


@pytest.mark.asyncio
async def test_reconciliation_required_blocks_message_replay_and_approval_resume() -> None:
    """不确定的审批恢复必须隔离，两个HTTP入口都不能再次resume同一Checkpoint。"""

    owner = "user-001"
    owner_headers = _customer_headers(owner)
    request_body = {
        "message": "为订单 SO100002 申请退货，原因：商品尺寸确实不合适",
        "idempotency_key": "quarantine-approval-0001",
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        created = await client.post("/api/v1/conversations", headers=owner_headers)
        conversation_id = UUID(created.json()["conversation_id"])
        initial = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=request_body,
            headers=owner_headers,
        )
        assert initial.status_code == 200
        assert initial.json()["execution_status"] == "approval_required"
        thread_id = UUID(initial.json()["thread_id"])
        repository: ConversationRepository = app.state.conversation_repository
        turn = repository.get_turn_by_workflow_thread(workflow_thread_id=thread_id)
        assert turn is not None
        approval_lease = repository.claim_turn_execution(
            conversation_id=conversation_id,
            turn_id=turn.turn_id,
            owner_user_id=owner,
            execution_kind=ExecutionKind.APPROVAL_RESUME,
            lease_seconds=30,
            decision_audit_event_id=str(uuid4()),
        )
        recovery = repository.recover_stale_turn_executions(
            now=approval_lease.lease_expires_at + timedelta(seconds=1),
            grace_seconds=0,
            accepted_stale_seconds=60,
            limit=10,
        )
        quarantined_lease = repository.get_turn_execution_lease(turn_id=turn.turn_id)
        graph_config = {"configurable": {"thread_id": str(thread_id)}}
        before = await app.state.service_graph.aget_state(graph_config)

        replay = await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json=request_body,
            headers=owner_headers,
        )
        approval = await client.post(
            f"/api/v1/approvals/{thread_id}",
            json={"approved": True, "comment": "隔离态不得恢复"},
            headers=_reviewer_headers("quarantine-reviewer-001"),
        )
        after = await app.state.service_graph.aget_state(graph_config)
        return_count = app.state.return_request_repository.count()

    assert recovery.approval_quarantined_count == 1
    assert quarantined_lease is not None
    assert quarantined_lease.kind == ExecutionKind.APPROVAL_RESUME
    assert quarantined_lease.state == ExecutionLeaseState.RECONCILIATION_REQUIRED
    assert replay.status_code == approval.status_code == 409
    assert replay.json() == approval.json() == {
        "detail": "该会话审批需要运维对账，不能自动恢复"
    }
    assert before.next == after.next
    assert before.interrupts == after.interrupts
    assert return_count == 0
