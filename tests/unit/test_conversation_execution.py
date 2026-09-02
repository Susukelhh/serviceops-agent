"""异步工作执行期间的租约心跳、fencing和取消传播测试。"""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta

import pytest

from serviceops_agent.application.conversation_execution import (
    run_with_execution_lease_heartbeat,
)
from serviceops_agent.domain.conversation import (
    ConversationExecutionLease,
    ConversationRecord,
    ExecutionKind,
)
from serviceops_agent.infrastructure.conversation_repository import (
    ConversationLeaseLostError,
    InMemoryConversationRepository,
)


def _claimed_execution() -> tuple[
    InMemoryConversationRepository,
    ConversationRecord,
    ConversationExecutionLease,
]:
    """创建一轮已经由初始执行租约接管的测试工作。"""

    repository = InMemoryConversationRepository()
    conversation = repository.create_conversation(
        owner_user_id="heartbeat-user",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id=conversation.owner_user_id,
        idempotency_key="heartbeat-test-0001",
        user_message="查询订单进度",
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id=conversation.owner_user_id,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=10,
    )
    return repository, conversation, lease


async def _run[ResultT](
    repository: InMemoryConversationRepository,
    conversation: ConversationRecord,
    lease: ConversationExecutionLease,
    work: Awaitable[ResultT],
    *,
    interval: float,
) -> tuple[ResultT, ConversationExecutionLease]:
    """用稳定测试参数调用心跳辅助器。"""

    return await run_with_execution_lease_heartbeat(
        repository=repository,
        conversation_id=conversation.conversation_id,
        owner_user_id=conversation.owner_user_id,
        lease=lease,
        lease_seconds=10,
        heartbeat_interval_seconds=interval,
        work=work,
    )


@pytest.mark.asyncio
async def test_fast_work_returns_without_renewing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作早于首个心跳完成时直接返回原租约和结果。"""

    repository, conversation, lease = _claimed_execution()
    renew_count = 0
    original_renew = repository.renew_turn_execution

    def counted_renew(**kwargs: object) -> ConversationExecutionLease:
        nonlocal renew_count
        renew_count += 1
        return original_renew(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "renew_turn_execution", counted_renew)

    result, latest_lease = await _run(
        repository,
        conversation,
        lease,
        asyncio.sleep(0, result="done"),
        interval=0.1,
    )

    assert result == "done"
    assert latest_lease == lease
    assert renew_count == 0


@pytest.mark.asyncio
async def test_long_work_renews_lease_at_least_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨过心跳间隔的工作返回仓库最后一次续租后的租约。"""

    repository, conversation, lease = _claimed_execution()
    renew_count = 0
    original_renew = repository.renew_turn_execution

    def counted_renew(**kwargs: object) -> ConversationExecutionLease:
        nonlocal renew_count
        renew_count += 1
        return original_renew(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "renew_turn_execution", counted_renew)

    result, latest_lease = await _run(
        repository,
        conversation,
        lease,
        asyncio.sleep(0.04, result=42),
        interval=0.01,
    )

    assert result == 42
    assert renew_count >= 1
    assert latest_lease.heartbeat_at >= lease.heartbeat_at
    assert latest_lease.claim_token == lease.claim_token


@pytest.mark.asyncio
async def test_lost_lease_cancels_and_waits_for_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """续租被fence后必须回收工作，并传播租约错误而不是陈旧结果。"""

    repository, conversation, lease = _claimed_execution()
    work_started = asyncio.Event()
    work_cancelled = asyncio.Event()

    def lose_lease(**kwargs: object) -> ConversationExecutionLease:
        _ = kwargs
        raise ConversationLeaseLostError

    async def cancellable_work() -> str:
        work_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            work_cancelled.set()
            raise

    monkeypatch.setattr(repository, "renew_turn_execution", lose_lease)

    with pytest.raises(ConversationLeaseLostError):
        await _run(
            repository,
            conversation,
            lease,
            cancellable_work(),
            interval=0.01,
        )

    assert work_started.is_set()
    assert work_cancelled.is_set()


@pytest.mark.asyncio
async def test_repository_failure_also_cancels_and_waits_for_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通续租故障同样意味着执行权未知，不能泄漏仍运行的工作任务。"""

    repository, conversation, lease = _claimed_execution()
    work_cancelled = asyncio.Event()

    def fail_renewal(**kwargs: object) -> ConversationExecutionLease:
        _ = kwargs
        raise OSError("lease store unavailable")

    async def cancellable_work() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            work_cancelled.set()
            raise

    monkeypatch.setattr(repository, "renew_turn_execution", fail_renewal)

    with pytest.raises(OSError, match="lease store unavailable"):
        await _run(
            repository,
            conversation,
            lease,
            cancellable_work(),
            interval=0.01,
        )

    assert work_cancelled.is_set()


@pytest.mark.asyncio
async def test_cancelling_heartbeat_helper_cancels_owned_work() -> None:
    """请求协程被取消时不能留下脱离租约管理的后台工作。"""

    repository, conversation, lease = _claimed_execution()
    work_started = asyncio.Event()
    work_cancelled = asyncio.Event()

    async def cancellable_work() -> None:
        work_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            work_cancelled.set()
            raise

    heartbeat_task = asyncio.create_task(
        _run(
            repository,
            conversation,
            lease,
            cancellable_work(),
            interval=1,
        )
    )
    await work_started.wait()
    heartbeat_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await heartbeat_task

    assert work_cancelled.is_set()


@pytest.mark.asyncio
async def test_work_exception_propagates_without_waiting_for_heartbeat() -> None:
    """业务工作自己的异常原样传播，辅助器不尝试完成租约。"""

    repository, conversation, lease = _claimed_execution()

    async def failing_work() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("work failed")

    with pytest.raises(RuntimeError, match="work failed"):
        await _run(
            repository,
            conversation,
            lease,
            failing_work(),
            interval=0.1,
        )

    stored_lease = repository.get_turn_execution_lease(turn_id=lease.turn_id)
    assert stored_lease == lease
