"""可选的PostgreSQL会话仓库集成测试；没有专用测试DSN时安全跳过。"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import sleep
from uuid import uuid4

import pytest
from alembic import command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

from serviceops_agent.domain.conversation import (
    ConversationDeletionPlan,
    ConversationMemory,
    ConversationStatus,
    ConversationTurnStatus,
    ConversationTurnUpdate,
    ExecutionKind,
    ExecutionLeaseState,
)
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationDeletionBusyError,
    ConversationLeaseConflictError,
    ConversationLeaseLostError,
    PostgresConversationRepository,
)
from serviceops_agent.infrastructure.migrate import build_alembic_config
from serviceops_agent.infrastructure.postgres_repository import PostgresConnectionPool

TEST_POSTGRES_DSN = os.getenv("SERVICEOPS_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="需要显式设置 SERVICEOPS_TEST_POSTGRES_DSN 才运行真实PostgreSQL测试",
)


def test_postgres_conversation_repository_persists_turn_and_memory() -> None:
    """迁移后的真实数据库应支持所有权、轮次幂等、状态推进和乐观记忆版本。"""

    assert TEST_POSTGRES_DSN is not None
    command.upgrade(
        build_alembic_config(postgres_dsn=SecretStr(TEST_POSTGRES_DSN)),
        "head",
    )
    pool: PostgresConnectionPool = ConnectionPool(
        conninfo=TEST_POSTGRES_DSN,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=2,
        open=False,
    )
    pool.open(wait=True)
    try:
        repository = PostgresConversationRepository(pool=pool)
        conversation = repository.create_conversation(
            owner_user_id=f"postgres-conversation-{uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        turn, replayed = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            idempotency_key=f"postgres-turn-{uuid4().hex}",
            user_message="SO100001能退货吗？",
        )
        replay, was_replayed = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            idempotency_key=turn.idempotency_key,
            user_message=turn.user_message,
        )
        repository.advance_turn(
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            owner_user_id=conversation.owner_user_id,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.ACCEPTED,
                status=ConversationTurnStatus.RUNNING,
                standalone_question=turn.user_message,
            ),
        )
        repository.update_memory(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
            expected_version=0,
            memory=ConversationMemory(memory_version=1, active_order_id="SO100001"),
        )

        restored = repository.get_conversation_for_owner(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
        )
        turns = repository.list_recent_turns(
            conversation_id=conversation.conversation_id,
            owner_user_id=conversation.owner_user_id,
        )
        workflow_turn = repository.get_turn_by_workflow_thread(
            workflow_thread_id=turn.workflow_thread_id,
        )

        assert replayed is False
        assert was_replayed is True
        assert replay.turn_id == turn.turn_id
        assert restored is not None
        assert restored.memory.active_order_id == "SO100001"
        assert [item.sequence_number for item in turns] == [1]
        assert turns[0].status == ConversationTurnStatus.RUNNING
        assert workflow_turn is not None
        assert workflow_turn.turn_id == turn.turn_id
    finally:
        pool.close()


def test_postgres_retention_is_two_phase_complete_and_busy_safe() -> None:
    """真实PostgreSQL应锁定会话、补偿封存计划，并拒绝活动图和缺失线程。"""

    assert TEST_POSTGRES_DSN is not None
    command.upgrade(
        build_alembic_config(postgres_dsn=SecretStr(TEST_POSTGRES_DSN)),
        "head",
    )
    pool: PostgresConnectionPool = ConnectionPool(
        conninfo=TEST_POSTGRES_DSN,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=2,
        open=False,
    )
    pool.open(wait=True)
    try:
        repository = PostgresConversationRepository(pool=pool)
        expiry = datetime.now(UTC) + timedelta(hours=1)
        expiring = repository.create_conversation(
            owner_user_id=f"postgres-expiring-{uuid4().hex}",
            expires_at=expiry,
        )
        expiring_turn, _ = repository.create_or_get_turn(
            conversation_id=expiring.conversation_id,
            owner_user_id=expiring.owner_user_id,
            idempotency_key=f"postgres-expiring-turn-{uuid4().hex}",
            user_message="这轮可以在TTL后清理",
        )
        repository.advance_turn(
            conversation_id=expiring.conversation_id,
            turn_id=expiring_turn.turn_id,
            owner_user_id=expiring.owner_user_id,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.ACCEPTED,
                status=ConversationTurnStatus.RUNNING,
            ),
        )
        repository.advance_turn(
            conversation_id=expiring.conversation_id,
            turn_id=expiring_turn.turn_id,
            owner_user_id=expiring.owner_user_id,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.RUNNING,
                status=ConversationTurnStatus.COMPLETED,
                assistant_answer="已完成。",
            ),
        )

        closed = repository.create_conversation(
            owner_user_id=f"postgres-closed-{uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        closed_plan = repository.prepare_conversation_deletion(
            conversation_id=closed.conversation_id,
            owner_user_id=closed.owner_user_id,
        )
        assert closed_plan is not None

        busy = repository.create_conversation(
            owner_user_id=f"postgres-busy-{uuid4().hex}",
            expires_at=expiry,
        )
        busy_turn, _ = repository.create_or_get_turn(
            conversation_id=busy.conversation_id,
            owner_user_id=busy.owner_user_id,
            idempotency_key=f"postgres-busy-turn-{uuid4().hex}",
            user_message="仍在执行",
        )
        with pytest.raises(ConversationDeletionBusyError):
            repository.prepare_conversation_deletion(
                conversation_id=busy.conversation_id,
                owner_user_id=busy.owner_user_id,
            )

        plans = repository.prepare_expired_conversation_deletions(
            now=expiry + timedelta(hours=1),
            limit=1000,
        )
        plans_by_id = {plan.conversation_id: plan for plan in plans}

        assert plans_by_id[expiring.conversation_id].prepared_status == (
            ConversationStatus.EXPIRED
        )
        assert plans_by_id[closed.conversation_id].prepared_status == (
            ConversationStatus.CLOSED
        )
        assert busy.conversation_id not in plans_by_id

        expiring_plan = plans_by_id[expiring.conversation_id]
        incomplete_plan = ConversationDeletionPlan(
            conversation_id=expiring_plan.conversation_id,
            owner_user_id=expiring_plan.owner_user_id,
            prepared_status=expiring_plan.prepared_status,
            workflow_thread_ids=[],
        )
        assert repository.delete_prepared_conversation(plan=incomplete_plan) is False
        assert repository.delete_prepared_conversation(plan=expiring_plan) is True
        assert repository.get_turn_by_workflow_thread(
            workflow_thread_id=expiring_turn.workflow_thread_id,
        ) is None
        assert repository.delete_prepared_conversation(plan=expiring_plan) is True
        assert repository.delete_prepared_conversation(plan=closed_plan) is True

        repository.advance_turn(
            conversation_id=busy.conversation_id,
            turn_id=busy_turn.turn_id,
            owner_user_id=busy.owner_user_id,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.ACCEPTED,
                status=ConversationTurnStatus.FAILED,
            ),
        )
        retry_plans = repository.prepare_expired_conversation_deletions(
            now=expiry + timedelta(hours=1),
            limit=1000,
        )
        retry_plan = next(
            plan for plan in retry_plans if plan.conversation_id == busy.conversation_id
        )
        assert repository.delete_prepared_conversation(plan=retry_plan) is True
    finally:
        pool.close()


def test_postgres_execution_lease_is_atomic_and_fences_stale_worker() -> None:
    """真实数据库的行锁、数据库时钟和generation必须共同阻止双执行及迟到写。"""

    assert TEST_POSTGRES_DSN is not None
    command.upgrade(
        build_alembic_config(postgres_dsn=SecretStr(TEST_POSTGRES_DSN)),
        "head",
    )
    pool: PostgresConnectionPool = ConnectionPool(
        conninfo=TEST_POSTGRES_DSN,
        kwargs={"row_factory": dict_row},
        min_size=1,
        max_size=3,
        open=False,
    )
    pool.open(wait=True)
    try:
        repository = PostgresConversationRepository(pool=pool)
        owner = f"postgres-lease-{uuid4().hex}"
        conversation = repository.create_conversation(
            owner_user_id=owner,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        turn, _ = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id=owner,
            idempotency_key=f"postgres-lease-turn-{uuid4().hex}",
            user_message="竞争领取执行租约",
        )

        def claim() -> bool:
            try:
                repository.claim_turn_execution(
                    conversation_id=conversation.conversation_id,
                    turn_id=turn.turn_id,
                    owner_user_id=owner,
                    execution_kind=ExecutionKind.INITIAL,
                    lease_seconds=30,
                )
                return True
            except ConversationLeaseConflictError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: claim(), range(2)))
        assert sorted(outcomes) == [False, True]

        lease = repository.get_turn_execution_lease(turn_id=turn.turn_id)
        assert lease is not None
        assert lease.state == ExecutionLeaseState.ACTIVE
        completed = repository.finish_turn_execution(
            conversation_id=conversation.conversation_id,
            owner_user_id=owner,
            lease=lease,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.RUNNING,
                status=ConversationTurnStatus.COMPLETED,
                assistant_answer="唯一worker完成。",
            ),
        )
        assert completed.status == ConversationTurnStatus.COMPLETED

        stale_turn, _ = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id=owner,
            idempotency_key=f"postgres-stale-turn-{uuid4().hex}",
            user_message="模拟过期worker",
        )
        stale_lease = repository.claim_turn_execution(
            conversation_id=conversation.conversation_id,
            turn_id=stale_turn.turn_id,
            owner_user_id=owner,
            execution_kind=ExecutionKind.INITIAL,
            lease_seconds=1,
        )
        sleep(1.1)
        recovery = repository.recover_stale_turn_executions(
            # PostgreSQL实现会校验时区，但以数据库时钟作为最终判断来源。
            now=datetime.now(UTC),
            grace_seconds=0,
            accepted_stale_seconds=60,
        )
        assert recovery.initial_failed_count >= 1
        recovered = repository.get_turn_execution_lease(turn_id=stale_turn.turn_id)
        assert recovered is not None
        assert recovered.state == ExecutionLeaseState.REVOKED
        assert recovered.fence_generation == stale_lease.fence_generation + 1
        with pytest.raises(ConversationLeaseLostError):
            repository.finish_turn_execution(
                conversation_id=conversation.conversation_id,
                owner_user_id=owner,
                lease=stale_lease,
                update=ConversationTurnUpdate(
                    expected_status=ConversationTurnStatus.RUNNING,
                    status=ConversationTurnStatus.COMPLETED,
                    assistant_answer="迟到写入。",
                ),
            )
    finally:
        pool.close()
