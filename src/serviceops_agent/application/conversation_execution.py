"""等待异步工作时续租会话执行权，并在fence丢失后取消陈旧工作。"""

import asyncio
from collections.abc import Awaitable
from functools import partial
from uuid import UUID

from serviceops_agent.domain.conversation import ConversationExecutionLease
from serviceops_agent.infrastructure.conversation_repository import ConversationRepository


async def _cancel_and_wait[ResultT](work_task: asyncio.Future[ResultT]) -> None:
    """取消并回收工作任务；工作自身的异常不能遮蔽调用方的取消或租约错误。"""

    work_task.cancel()
    try:
        await work_task
    except asyncio.CancelledError:
        pass
    except Exception:
        # 任务可能在续租失败的同时先抛出业务异常；此时租约错误才是安全边界。
        pass


async def run_with_execution_lease_heartbeat[ResultT](
    *,
    repository: ConversationRepository,
    conversation_id: UUID,
    owner_user_id: str,
    lease: ConversationExecutionLease,
    lease_seconds: int,
    heartbeat_interval_seconds: float,
    work: Awaitable[ResultT],
) -> tuple[ResultT, ConversationExecutionLease]:
    """等待工作并定期续租，返回工作结果及调用方可用于终结的最新租约。

    本函数只管理执行期间的心跳，不调用 ``finish_turn_execution``。一旦当前代次
    被fence或认领状态冲突，它会先取消并等待工作结束，再传播原租约异常，避免调用方
    把陈旧工作结果写入终态。
    """

    if lease_seconds <= 0:
        raise ValueError("lease_seconds必须大于0")
    if heartbeat_interval_seconds <= 0:
        raise ValueError("heartbeat_interval_seconds必须大于0")

    latest_lease = lease
    work_task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait(
                {work_task},
                timeout=heartbeat_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work_task in done:
                return await work_task, latest_lease

            try:
                latest_lease = await asyncio.to_thread(
                    partial(
                        repository.renew_turn_execution,
                        conversation_id=conversation_id,
                        owner_user_id=owner_user_id,
                        lease=latest_lease,
                        lease_seconds=lease_seconds,
                    )
                )
            except Exception:
                # 任意仓库故障都会让调用方无法证明自己仍持有执行权。
                await _cancel_and_wait(work_task)
                raise
    except asyncio.CancelledError:
        await _cancel_and_wait(work_task)
        raise
