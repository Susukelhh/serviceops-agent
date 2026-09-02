"""多轮会话与轮次仓库协议，以及内存、SQLite、PostgreSQL实现。"""

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from serviceops_agent.domain.conversation import (
    ConversationDeletionPlan,
    ConversationExecutionLease,
    ConversationExecutionRecoveryResult,
    ConversationMemory,
    ConversationRecord,
    ConversationStatus,
    ConversationTurnRecord,
    ConversationTurnStatus,
    ConversationTurnUpdate,
    ExecutionKind,
    ExecutionLeaseState,
)
from serviceops_agent.infrastructure.postgres_repository import PostgresConnectionPool


class ConversationRepositoryError(Exception):
    """会话仓库可预期冲突的共同基类。"""


class ConversationUnavailableError(ConversationRepositoryError):
    """会话不存在、不属于当前用户、已关闭或已经过期。"""


class ConversationDeletionBusyError(ConversationRepositoryError):
    """会话仍有可能写入业务结果的活动轮次，暂时不能删除。"""


class ConversationIdempotencyConflictError(ConversationRepositoryError):
    """同一会话中的幂等键被用于不同用户消息。"""


class ConversationVersionConflictError(ConversationRepositoryError):
    """会话记忆已被其他请求更新，调用方必须重新加载。"""


class ConversationTurnConflictError(ConversationRepositoryError):
    """轮次不存在、归属不匹配或状态转换前提已经变化。"""


class ConversationLeaseConflictError(ConversationRepositoryError):
    """轮次阶段不允许认领新租约，或仍有未完成的租约记录。"""


class ConversationLeaseLostError(ConversationRepositoryError):
    """调用方持有的token或generation已经被新的恢复代次fence。"""


class ConversationRepository(Protocol):
    """API与上下文构建器依赖的同步会话仓库接口。"""

    def create_conversation(
        self,
        *,
        owner_user_id: str,
        expires_at: datetime,
    ) -> ConversationRecord:
        """创建一段归属于可信系统身份的新会话。"""

    def get_conversation_for_owner(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord | None:
        """只向所有者返回会话；不存在和越权都返回None。"""

    def create_or_get_turn(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> tuple[ConversationTurnRecord, bool]:
        """原子分配轮次序号，或返回同键同消息的幂等记录。"""

    def list_recent_turns(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        limit: int = 6,
    ) -> list[ConversationTurnRecord]:
        """按时间正序返回最近的有限轮次。"""

    def get_turn_for_owner(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
    ) -> ConversationTurnRecord | None:
        """只向会话所有者返回指定轮次；不存在和越权使用同一空结果。"""

    def get_turn_by_workflow_thread(
        self,
        *,
        workflow_thread_id: UUID,
    ) -> ConversationTurnRecord | None:
        """按唯一工作流线程查找会话轮次，供内部审批恢复同步索引。"""

    def update_memory(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        expected_version: int,
        memory: ConversationMemory,
    ) -> ConversationRecord:
        """使用乐观版本原子替换结构化会话记忆。"""

    def advance_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        """按预期状态原子推进一轮工作流。"""

    def claim_turn_execution(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        execution_kind: ExecutionKind,
        lease_seconds: int,
        decision_audit_event_id: str | None = None,
    ) -> ConversationExecutionLease:
        """原子认领初始执行或审批恢复，并创建新一代fencing token。"""

    def renew_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        lease_seconds: int,
    ) -> ConversationExecutionLease:
        """仅允许当前活动代次续租；刚过期但尚未被恢复任务fence仍可续租。"""

    def finish_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        """校验活动fence后原子写轮次结果并释放租约。"""

    def get_turn_execution_lease(
        self,
        *,
        turn_id: UUID,
    ) -> ConversationExecutionLease | None:
        """读取一轮的最新租约代次，供处理中响应和恢复诊断使用。"""

    def recover_stale_turn_executions(
        self,
        *,
        now: datetime,
        grace_seconds: int,
        accepted_stale_seconds: int,
        limit: int = 100,
    ) -> ConversationExecutionRecoveryResult:
        """fence陈旧执行；初始执行失败关闭，审批恢复进入人工对账。"""

    def count_conversations(self) -> int:
        """返回仓库中的会话数量，供测试与健康诊断使用。"""

    def prepare_conversation_deletion(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationDeletionPlan | None:
        """原子关闭所有者会话并返回需要删除的全部工作流线程。"""

    def prepare_expired_conversation_deletions(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[ConversationDeletionPlan]:
        """原子标记一批到期会话并返回清理计划。"""

    def delete_prepared_conversation(self, *, plan: ConversationDeletionPlan) -> bool:
        """物理删除匹配计划的会话；已被同计划并发删除也视为幂等成功。"""


def _require_active_conversation(record: ConversationRecord) -> None:
    """统一拒绝已关闭或过期会话。"""

    if record.status != ConversationStatus.ACTIVE or record.expires_at <= datetime.now(UTC):
        raise ConversationUnavailableError


def _require_turn_progress_allowed(record: ConversationRecord) -> None:
    """允许已接纳轮次跨TTL收尾，但拒绝已封存会话继续写结果。"""

    if record.status != ConversationStatus.ACTIVE:
        raise ConversationTurnConflictError


def _normalize_expiry(expires_at: datetime) -> datetime:
    """统一保存UTC期限，避免SQLite对不同时区ISO文本作错误字典序比较。"""

    if expires_at.utcoffset() is None:
        raise ValueError("会话期限必须包含时区")
    return expires_at.astimezone(UTC)


def _turn_is_active(turn: ConversationTurnRecord) -> bool:
    """仅正在启动或执行的工作流会与Checkpoint删除发生竞争。"""

    return turn.status in {
        ConversationTurnStatus.ACCEPTED,
        ConversationTurnStatus.RUNNING,
    }


def _validate_lease_seconds(lease_seconds: int) -> None:
    """限制租约长度，避免零时长或无界占有。"""

    if not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds必须位于1到3600")


def _validate_recovery_request(
    *,
    now: datetime,
    grace_seconds: int,
    accepted_stale_seconds: int,
    limit: int,
) -> None:
    """恢复扫描只接受有时区时钟与有限批次。"""

    if now.utcoffset() is None:
        raise ValueError("恢复时间必须包含时区")
    if not 0 <= grace_seconds <= 86_400:
        raise ValueError("grace_seconds必须位于0到86400")
    if not 1 <= accepted_stale_seconds <= 604_800:
        raise ValueError("accepted_stale_seconds必须位于1到604800")
    if not 1 <= limit <= 1000:
        raise ValueError("恢复limit必须位于1到1000")


def _new_execution_lease(
    *,
    turn_id: UUID,
    kind: ExecutionKind,
    state: ExecutionLeaseState,
    fence_generation: int,
    now: datetime,
    lease_seconds: int,
    decision_audit_event_id: str | None,
) -> ConversationExecutionLease:
    """构造不复用token的新租约代次。"""

    return ConversationExecutionLease(
        turn_id=turn_id,
        kind=kind,
        state=state,
        claim_token=uuid4(),
        fence_generation=fence_generation,
        decision_audit_event_id=decision_audit_event_id,
        claimed_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
    )


def _lease_matches(
    stored: ConversationExecutionLease,
    presented: ConversationExecutionLease,
) -> bool:
    """token、generation和入口类型共同组成不可猜测fence。"""

    return (
        stored.turn_id == presented.turn_id
        and stored.kind == presented.kind
        and stored.claim_token == presented.claim_token
        and stored.fence_generation == presented.fence_generation
        and stored.state == ExecutionLeaseState.ACTIVE
        and presented.state == ExecutionLeaseState.ACTIVE
    )


def _validate_claim_source(
    *,
    turn: ConversationTurnRecord,
    execution_kind: ExecutionKind,
    decision_audit_event_id: str | None,
) -> None:
    """初始执行与审批恢复只能从各自稳定阶段开始。"""

    if execution_kind == ExecutionKind.INITIAL:
        if (
            turn.status != ConversationTurnStatus.ACCEPTED
            or decision_audit_event_id is not None
        ):
            raise ConversationLeaseConflictError
        return
    if (
        turn.status != ConversationTurnStatus.WAITING_APPROVAL
        or decision_audit_event_id is None
    ):
        raise ConversationLeaseConflictError


def _validate_finish_source(
    *,
    turn: ConversationTurnRecord,
    lease: ConversationExecutionLease,
    update: ConversationTurnUpdate,
) -> None:
    """完成写入必须与租约入口及轮次当前阶段完全一致。"""

    expected_status = (
        ConversationTurnStatus.RUNNING
        if lease.kind == ExecutionKind.INITIAL
        else ConversationTurnStatus.WAITING_APPROVAL
    )
    if turn.status != expected_status or update.expected_status != expected_status:
        raise ConversationLeaseLostError
    if (
        lease.kind == ExecutionKind.APPROVAL_RESUME
        and update.status
        not in {ConversationTurnStatus.COMPLETED, ConversationTurnStatus.FAILED}
    ):
        raise ConversationLeaseConflictError


def _validate_memory_version(*, expected_version: int, memory: ConversationMemory) -> None:
    """新记忆必须恰好在调用方读取的版本上加一。"""

    if expected_version < 0 or memory.memory_version != expected_version + 1:
        raise ConversationVersionConflictError


def _validate_retention_request(*, now: datetime, limit: int) -> None:
    """生命周期批次必须使用明确时区和有限数量。"""

    if now.utcoffset() is None:
        raise ValueError("生命周期时间必须包含时区")
    if not 1 <= limit <= 1000:
        raise ValueError("生命周期limit必须位于1到1000")


def _apply_turn_update(
    existing: ConversationTurnRecord,
    update: ConversationTurnUpdate,
    *,
    updated_at: datetime,
) -> ConversationTurnRecord:
    """保留不可变标识，仅替换经过Schema约束的结果字段。"""

    if existing.status != update.expected_status:
        raise ConversationTurnConflictError
    return existing.model_copy(
        update={
            "status": update.status,
            "standalone_question": update.standalone_question,
            "assistant_answer": update.assistant_answer,
            "intent": update.intent,
            "verified_order_ids": update.verified_order_ids,
            "cited_document_ids": update.cited_document_ids,
            "updated_at": updated_at,
        }
    )


class InMemoryConversationRepository:
    """进程内线程安全会话仓库，用于单元测试和无数据库模式。"""

    def __init__(self) -> None:
        self._conversations: dict[UUID, ConversationRecord] = {}
        self._turns: dict[UUID, ConversationTurnRecord] = {}
        self._turn_ids_by_conversation: dict[UUID, list[UUID]] = {}
        self._turn_id_by_idempotency: dict[tuple[UUID, str], UUID] = {}
        self._turn_id_by_workflow_thread: dict[UUID, UUID] = {}
        self._execution_leases: dict[UUID, ConversationExecutionLease] = {}
        self._lock = Lock()

    def create_conversation(
        self,
        *,
        owner_user_id: str,
        expires_at: datetime,
    ) -> ConversationRecord:
        now = datetime.now(UTC)
        record = ConversationRecord(
            conversation_id=uuid4(),
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
            expires_at=_normalize_expiry(expires_at),
        )
        with self._lock:
            self._conversations[record.conversation_id] = record
            self._turn_ids_by_conversation[record.conversation_id] = []
        return record

    def get_conversation_for_owner(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord | None:
        with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None or record.owner_user_id != owner_user_id:
                return None
            return record

    def create_or_get_turn(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> tuple[ConversationTurnRecord, bool]:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.owner_user_id != owner_user_id:
                raise ConversationUnavailableError
            _require_active_conversation(conversation)
            idempotency_index = (conversation_id, idempotency_key)
            existing_turn_id = self._turn_id_by_idempotency.get(idempotency_index)
            if existing_turn_id is not None:
                existing = self._turns[existing_turn_id]
                if existing.user_message != user_message:
                    raise ConversationIdempotencyConflictError
                return existing, True
            turn_ids = self._turn_ids_by_conversation[conversation_id]
            now = datetime.now(UTC)
            turn = ConversationTurnRecord(
                turn_id=uuid4(),
                conversation_id=conversation_id,
                workflow_thread_id=uuid4(),
                sequence_number=len(turn_ids) + 1,
                idempotency_key=idempotency_key,
                user_message=user_message,
                created_at=now,
                updated_at=now,
            )
            self._turns[turn.turn_id] = turn
            turn_ids.append(turn.turn_id)
            self._turn_id_by_idempotency[idempotency_index] = turn.turn_id
            self._turn_id_by_workflow_thread[turn.workflow_thread_id] = turn.turn_id
            return turn, False

    def list_recent_turns(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        limit: int = 6,
    ) -> list[ConversationTurnRecord]:
        if not 1 <= limit <= 50:
            raise ValueError("会话轮次limit必须位于1到50")
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None or conversation.owner_user_id != owner_user_id:
                raise ConversationUnavailableError
            turn_ids = self._turn_ids_by_conversation[conversation_id][-limit:]
            return [self._turns[turn_id] for turn_id in turn_ids]

    def get_turn_for_owner(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
    ) -> ConversationTurnRecord | None:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            turn = self._turns.get(turn_id)
            if (
                conversation is None
                or conversation.owner_user_id != owner_user_id
                or turn is None
                or turn.conversation_id != conversation_id
            ):
                return None
            return turn

    def get_turn_by_workflow_thread(
        self,
        *,
        workflow_thread_id: UUID,
    ) -> ConversationTurnRecord | None:
        with self._lock:
            turn_id = self._turn_id_by_workflow_thread.get(workflow_thread_id)
            return None if turn_id is None else self._turns[turn_id]

    def update_memory(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        expected_version: int,
        memory: ConversationMemory,
    ) -> ConversationRecord:
        _validate_memory_version(expected_version=expected_version, memory=memory)
        with self._lock:
            existing = self._conversations.get(conversation_id)
            if existing is None or existing.owner_user_id != owner_user_id:
                raise ConversationUnavailableError
            _require_active_conversation(existing)
            if existing.memory.memory_version != expected_version:
                raise ConversationVersionConflictError
            updated = existing.model_copy(
                update={"memory": memory, "updated_at": datetime.now(UTC)}
            )
            self._conversations[conversation_id] = updated
            return updated

    def advance_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        with self._lock:
            conversation = self._conversations.get(conversation_id)
            existing = self._turns.get(turn_id)
            if (
                conversation is None
                or conversation.owner_user_id != owner_user_id
                or existing is None
                or existing.conversation_id != conversation_id
            ):
                raise ConversationTurnConflictError
            _require_turn_progress_allowed(conversation)
            if turn_id in self._execution_leases:
                raise ConversationTurnConflictError
            updated = _apply_turn_update(existing, update, updated_at=datetime.now(UTC))
            self._turns[turn_id] = updated
            return updated

    def _execution_target(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
    ) -> tuple[ConversationRecord, ConversationTurnRecord]:
        conversation = self._conversations.get(conversation_id)
        turn = self._turns.get(turn_id)
        if (
            conversation is None
            or conversation.owner_user_id != owner_user_id
            or turn is None
            or turn.conversation_id != conversation_id
        ):
            raise ConversationLeaseConflictError
        try:
            _require_turn_progress_allowed(conversation)
        except ConversationTurnConflictError as error:
            raise ConversationLeaseConflictError from error
        return conversation, turn

    def claim_turn_execution(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        execution_kind: ExecutionKind,
        lease_seconds: int,
        decision_audit_event_id: str | None = None,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        with self._lock:
            _, turn = self._execution_target(
                conversation_id=conversation_id,
                turn_id=turn_id,
                owner_user_id=owner_user_id,
            )
            _validate_claim_source(
                turn=turn,
                execution_kind=execution_kind,
                decision_audit_event_id=decision_audit_event_id,
            )
            previous = self._execution_leases.get(turn_id)
            if previous is not None and previous.state in {
                ExecutionLeaseState.ACTIVE,
                ExecutionLeaseState.RECONCILIATION_REQUIRED,
            }:
                raise ConversationLeaseConflictError
            now = datetime.now(UTC)
            lease = _new_execution_lease(
                turn_id=turn_id,
                kind=execution_kind,
                state=ExecutionLeaseState.ACTIVE,
                fence_generation=(
                    1 if previous is None else previous.fence_generation + 1
                ),
                now=now,
                lease_seconds=lease_seconds,
                decision_audit_event_id=decision_audit_event_id,
            )
            if execution_kind == ExecutionKind.INITIAL:
                self._turns[turn_id] = turn.model_copy(
                    update={
                        "status": ConversationTurnStatus.RUNNING,
                        "updated_at": now,
                    }
                )
            self._execution_leases[turn_id] = lease
            return lease

    def renew_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        lease_seconds: int,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        with self._lock:
            try:
                _, turn = self._execution_target(
                    conversation_id=conversation_id,
                    turn_id=lease.turn_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationLeaseConflictError as error:
                raise ConversationLeaseLostError from error
            stored = self._execution_leases.get(lease.turn_id)
            if stored is None or not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            expected_status = (
                ConversationTurnStatus.RUNNING
                if stored.kind == ExecutionKind.INITIAL
                else ConversationTurnStatus.WAITING_APPROVAL
            )
            if turn.status != expected_status:
                raise ConversationLeaseLostError
            now = datetime.now(UTC)
            renewed = stored.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            self._execution_leases[lease.turn_id] = renewed
            return renewed

    def finish_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        with self._lock:
            try:
                _, turn = self._execution_target(
                    conversation_id=conversation_id,
                    turn_id=lease.turn_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationLeaseConflictError as error:
                raise ConversationLeaseLostError from error
            stored = self._execution_leases.get(lease.turn_id)
            if stored is None or not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            _validate_finish_source(turn=turn, lease=stored, update=update)
            now = datetime.now(UTC)
            updated = _apply_turn_update(turn, update, updated_at=now)
            self._turns[lease.turn_id] = updated
            self._execution_leases[lease.turn_id] = stored.model_copy(
                update={"state": ExecutionLeaseState.RELEASED, "heartbeat_at": now}
            )
            return updated

    def get_turn_execution_lease(
        self,
        *,
        turn_id: UUID,
    ) -> ConversationExecutionLease | None:
        with self._lock:
            return self._execution_leases.get(turn_id)

    def recover_stale_turn_executions(
        self,
        *,
        now: datetime,
        grace_seconds: int,
        accepted_stale_seconds: int,
        limit: int = 100,
    ) -> ConversationExecutionRecoveryResult:
        _validate_recovery_request(
            now=now,
            grace_seconds=grace_seconds,
            accepted_stale_seconds=accepted_stale_seconds,
            limit=limit,
        )
        stable_now = now.astimezone(UTC)
        lease_cutoff = stable_now - timedelta(seconds=grace_seconds)
        legacy_cutoff = stable_now - timedelta(seconds=accepted_stale_seconds)
        with self._lock:
            candidates: list[tuple[ConversationTurnRecord, ConversationExecutionLease | None]] = []
            for turn in sorted(self._turns.values(), key=lambda item: item.updated_at):
                conversation = self._conversations.get(turn.conversation_id)
                if conversation is None or conversation.status != ConversationStatus.ACTIVE:
                    continue
                lease = self._execution_leases.get(turn.turn_id)
                stale_active = (
                    lease is not None
                    and lease.state == ExecutionLeaseState.ACTIVE
                    and lease.lease_expires_at <= lease_cutoff
                )
                stale_legacy = (
                    turn.status in {
                        ConversationTurnStatus.ACCEPTED,
                        ConversationTurnStatus.RUNNING,
                    }
                    and turn.updated_at <= legacy_cutoff
                    and (
                        lease is None
                        or lease.state
                        in {ExecutionLeaseState.RELEASED, ExecutionLeaseState.REVOKED}
                    )
                )
                if stale_active or stale_legacy:
                    candidates.append((turn, lease))
                if len(candidates) == limit:
                    break

            accepted_failed = 0
            initial_failed = 0
            approval_quarantined = 0
            legacy_manual_review = 0
            for turn, lease in candidates:
                if lease is None or lease.state != ExecutionLeaseState.ACTIVE:
                    if turn.status == ConversationTurnStatus.ACCEPTED:
                        self._turns[turn.turn_id] = turn.model_copy(
                            update={
                                "status": ConversationTurnStatus.FAILED,
                                "updated_at": stable_now,
                            }
                        )
                        accepted_failed += 1
                    else:
                        # 人工处置项仍保持RUNNING，但刷新排序时间，避免小批次反复
                        # 扫描同一遗留项而饿死后续候选。
                        self._turns[turn.turn_id] = turn.model_copy(
                            update={"updated_at": stable_now}
                        )
                        legacy_manual_review += 1
                    continue

                recovered_state = (
                    ExecutionLeaseState.REVOKED
                    if lease.kind == ExecutionKind.INITIAL
                    else ExecutionLeaseState.RECONCILIATION_REQUIRED
                )
                recovered = _new_execution_lease(
                    turn_id=turn.turn_id,
                    kind=lease.kind,
                    state=recovered_state,
                    fence_generation=lease.fence_generation + 1,
                    now=stable_now,
                    lease_seconds=0,
                    decision_audit_event_id=lease.decision_audit_event_id,
                )
                self._execution_leases[turn.turn_id] = recovered
                if (
                    lease.kind == ExecutionKind.INITIAL
                    and turn.status == ConversationTurnStatus.RUNNING
                ):
                    self._turns[turn.turn_id] = turn.model_copy(
                        update={
                            "status": ConversationTurnStatus.FAILED,
                            "updated_at": stable_now,
                        }
                    )
                    initial_failed += 1
                elif lease.kind == ExecutionKind.APPROVAL_RESUME:
                    approval_quarantined += 1
                else:
                    self._turns[turn.turn_id] = turn.model_copy(
                        update={"updated_at": stable_now}
                    )
                    legacy_manual_review += 1

            return ConversationExecutionRecoveryResult(
                scanned_count=len(candidates),
                accepted_failed_count=accepted_failed,
                initial_failed_count=initial_failed,
                approval_quarantined_count=approval_quarantined,
                legacy_manual_review_count=legacy_manual_review,
            )

    def count_conversations(self) -> int:
        with self._lock:
            return len(self._conversations)

    def _turn_blocks_deletion(self, turn_id: UUID) -> bool:
        turn = self._turns[turn_id]
        if _turn_is_active(turn):
            return True
        lease = self._execution_leases.get(turn_id)
        return lease is not None and (
            lease.state == ExecutionLeaseState.ACTIVE
            or (
                lease.kind == ExecutionKind.APPROVAL_RESUME
                and lease.state == ExecutionLeaseState.RECONCILIATION_REQUIRED
            )
        )

    def _deletion_plan(self, record: ConversationRecord) -> ConversationDeletionPlan:
        turn_ids = self._turn_ids_by_conversation.get(record.conversation_id, [])
        return ConversationDeletionPlan(
            conversation_id=record.conversation_id,
            owner_user_id=record.owner_user_id,
            prepared_status=record.status,
            workflow_thread_ids=[
                self._turns[turn_id].workflow_thread_id for turn_id in turn_ids
            ],
        )

    def prepare_conversation_deletion(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationDeletionPlan | None:
        with self._lock:
            existing = self._conversations.get(conversation_id)
            if existing is None or existing.owner_user_id != owner_user_id:
                return None
            turn_ids = self._turn_ids_by_conversation.get(conversation_id, [])
            if any(self._turn_blocks_deletion(turn_id) for turn_id in turn_ids):
                raise ConversationDeletionBusyError
            prepared = existing.model_copy(
                update={
                    "status": ConversationStatus.CLOSED,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._conversations[conversation_id] = prepared
            return self._deletion_plan(prepared)

    def prepare_expired_conversation_deletions(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[ConversationDeletionPlan]:
        _validate_retention_request(now=now, limit=limit)
        stable_now = now.astimezone(UTC)
        with self._lock:
            candidates = sorted(
                (
                    record
                    for record in self._conversations.values()
                    if (
                        record.status
                        in {ConversationStatus.CLOSED, ConversationStatus.EXPIRED}
                        or (
                            record.status == ConversationStatus.ACTIVE
                            and record.expires_at <= stable_now
                        )
                    )
                    and not any(
                        self._turn_blocks_deletion(turn_id)
                        for turn_id in self._turn_ids_by_conversation.get(
                            record.conversation_id,
                            [],
                        )
                    )
                ),
                # 每次领取都会刷新updated_at；失败计划因此移到队尾，不会永久饿死新到期会话。
                key=lambda record: (
                    record.updated_at,
                    record.expires_at,
                    str(record.conversation_id),
                ),
            )[:limit]
            plans: list[ConversationDeletionPlan] = []
            for record in candidates:
                prepared_status = (
                    ConversationStatus.EXPIRED
                    if record.status == ConversationStatus.ACTIVE
                    else record.status
                )
                prepared = record.model_copy(
                    update={"status": prepared_status, "updated_at": stable_now}
                )
                self._conversations[record.conversation_id] = prepared
                plans.append(self._deletion_plan(prepared))
            return plans

    def delete_prepared_conversation(self, *, plan: ConversationDeletionPlan) -> bool:
        with self._lock:
            existing = self._conversations.get(plan.conversation_id)
            if existing is None:
                return True
            if (
                existing.owner_user_id != plan.owner_user_id
                or existing.status != plan.prepared_status
            ):
                return False
            turn_ids = self._turn_ids_by_conversation.get(plan.conversation_id, [])
            turns = [self._turns.get(turn_id) for turn_id in turn_ids]
            if any(turn is None for turn in turns):
                return False
            current_thread_ids = {
                turn.workflow_thread_id for turn in turns if turn is not None
            }
            if current_thread_ids != set(plan.workflow_thread_ids):
                return False
            if any(
                self._turn_blocks_deletion(turn.turn_id)
                for turn in turns
                if turn is not None
            ):
                return False
            self._turn_ids_by_conversation.pop(plan.conversation_id, None)
            for turn_id in turn_ids:
                self._execution_leases.pop(turn_id, None)
                turn = self._turns.pop(turn_id)
                self._turn_id_by_idempotency.pop(
                    (turn.conversation_id, turn.idempotency_key),
                    None,
                )
                self._turn_id_by_workflow_thread.pop(turn.workflow_thread_id, None)
            self._conversations.pop(plan.conversation_id)
            return True


CONVERSATION_SELECT_COLUMNS = """
    conversation_id, owner_user_id, status, memory_json, memory_version,
    created_at, updated_at, expires_at
"""

TURN_SELECT_COLUMNS = """
    turn_id, conversation_id, workflow_thread_id, sequence_number,
    idempotency_key, status, user_message, standalone_question,
    assistant_answer, intent, verified_order_ids_json,
    cited_document_ids_json, created_at, updated_at
"""

LEASE_SELECT_COLUMNS = """
    turn_id, kind, state, claim_token, fence_generation,
    decision_audit_event_id, claimed_at, heartbeat_at, lease_expires_at
"""


def _conversation_from_row(row: Mapping[str, Any]) -> ConversationRecord:
    """把数据库行恢复为会话领域模型并校验记忆版本一致性。"""

    memory_payload = row["memory_json"]
    if isinstance(memory_payload, str):
        memory_payload = json.loads(memory_payload)
    memory = ConversationMemory.model_validate(memory_payload)
    if memory.memory_version != int(row["memory_version"]):
        raise ConversationVersionConflictError
    return ConversationRecord.model_validate(
        {
            "conversation_id": row["conversation_id"],
            "owner_user_id": row["owner_user_id"],
            "status": row["status"],
            "memory": memory,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }
    )


def _turn_from_row(row: Mapping[str, Any]) -> ConversationTurnRecord:
    """把数据库行恢复为轮次领域模型。"""

    verified_order_ids = row["verified_order_ids_json"]
    cited_document_ids = row["cited_document_ids_json"]
    if isinstance(verified_order_ids, str):
        verified_order_ids = json.loads(verified_order_ids)
    if isinstance(cited_document_ids, str):
        cited_document_ids = json.loads(cited_document_ids)
    payload = dict(row)
    payload.pop("verified_order_ids_json")
    payload.pop("cited_document_ids_json")
    payload["verified_order_ids"] = verified_order_ids
    payload["cited_document_ids"] = cited_document_ids
    return ConversationTurnRecord.model_validate(payload)


def _lease_from_row(row: Mapping[str, Any]) -> ConversationExecutionLease:
    """把低敏租约行恢复为经过时间线约束的领域对象。"""

    return ConversationExecutionLease.model_validate(dict(row))


class SQLiteConversationRepository:
    """使用SQLite事务、唯一键和乐观版本的本地持久化会话仓库。"""

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path.resolve()
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._setup_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._database_path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _setup_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'closed', 'expired')),
                    memory_json TEXT NOT NULL,
                    memory_version INTEGER NOT NULL CHECK (memory_version >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
                ON conversations (owner_user_id, updated_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversations_status_expires
                ON conversations (status, expires_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                    workflow_thread_id TEXT NOT NULL UNIQUE,
                    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 1),
                    idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('accepted', 'running', 'waiting_approval', 'completed', 'failed')
                    ),
                    user_message TEXT NOT NULL,
                    standalone_question TEXT,
                    assistant_answer TEXT,
                    intent TEXT,
                    verified_order_ids_json TEXT NOT NULL,
                    cited_document_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (conversation_id, sequence_number),
                    UNIQUE (conversation_id, idempotency_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_turns_recent
                ON conversation_turns (conversation_id, sequence_number DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_execution_leases (
                    turn_id TEXT PRIMARY KEY
                        REFERENCES conversation_turns(turn_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('initial', 'approval_resume')),
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'active', 'released', 'revoked', 'reconciliation_required'
                        )
                    ),
                    claim_token TEXT NOT NULL UNIQUE,
                    fence_generation INTEGER NOT NULL CHECK (fence_generation >= 1),
                    decision_audit_event_id TEXT,
                    claimed_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    CHECK (heartbeat_at >= claimed_at),
                    CHECK (state <> 'active' OR lease_expires_at > heartbeat_at),
                    CHECK (
                        (kind = 'initial' AND decision_audit_event_id IS NULL)
                        OR
                        (kind = 'approval_resume' AND decision_audit_event_id IS NOT NULL)
                    )
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_conversation_execution_leases_state_expiry
                ON conversation_execution_leases (state, lease_expires_at)
                """
            )
        finally:
            connection.close()

    def create_conversation(
        self,
        *,
        owner_user_id: str,
        expires_at: datetime,
    ) -> ConversationRecord:
        now = datetime.now(UTC)
        record = ConversationRecord(
            conversation_id=uuid4(),
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
            expires_at=_normalize_expiry(expires_at),
        )
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, owner_user_id, status, memory_json, memory_version,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record.conversation_id),
                    record.owner_user_id,
                    record.status.value,
                    record.memory.model_dump_json(),
                    record.memory.memory_version,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.expires_at.isoformat(),
                ),
            )
            return record
        finally:
            connection.close()

    def get_conversation_for_owner(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE conversation_id = ? AND owner_user_id = ?",
                (str(conversation_id), owner_user_id),
            ).fetchone()
            return None if row is None else _conversation_from_row(row)
        finally:
            connection.close()

    def _active_conversation_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord:
        row = connection.execute(
            f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
            "WHERE conversation_id = ? AND owner_user_id = ?",
            (str(conversation_id), owner_user_id),
        ).fetchone()
        if row is None:
            raise ConversationUnavailableError
        record = _conversation_from_row(row)
        _require_active_conversation(record)
        return record

    def _conversation_for_turn_progress_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord:
        """锁定写事务内的会话，只按状态判断既有轮次能否收尾。"""

        row = connection.execute(
            f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
            "WHERE conversation_id = ? AND owner_user_id = ?",
            (str(conversation_id), owner_user_id),
        ).fetchone()
        if row is None:
            raise ConversationTurnConflictError
        record = _conversation_from_row(row)
        _require_turn_progress_allowed(record)
        return record

    def create_or_get_turn(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> tuple[ConversationTurnRecord, bool]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._active_conversation_in_transaction(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            existing_row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = ? AND idempotency_key = ?",
                (str(conversation_id), idempotency_key),
            ).fetchone()
            if existing_row is not None:
                existing = _turn_from_row(existing_row)
                if existing.user_message != user_message:
                    raise ConversationIdempotencyConflictError
                connection.commit()
                return existing, True
            sequence_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence_number), 0) + 1 "
                    "FROM conversation_turns WHERE conversation_id = ?",
                    (str(conversation_id),),
                ).fetchone()[0]
            )
            now = datetime.now(UTC)
            turn = ConversationTurnRecord(
                turn_id=uuid4(),
                conversation_id=conversation_id,
                workflow_thread_id=uuid4(),
                sequence_number=sequence_number,
                idempotency_key=idempotency_key,
                user_message=user_message,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO conversation_turns (
                    turn_id, conversation_id, workflow_thread_id, sequence_number,
                    idempotency_key, status, user_message, standalone_question,
                    assistant_answer, intent, verified_order_ids_json,
                    cited_document_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(turn.turn_id),
                    str(turn.conversation_id),
                    str(turn.workflow_thread_id),
                    turn.sequence_number,
                    turn.idempotency_key,
                    turn.status.value,
                    turn.user_message,
                    None,
                    None,
                    None,
                    "[]",
                    "[]",
                    turn.created_at.isoformat(),
                    turn.updated_at.isoformat(),
                ),
            )
            connection.commit()
            return turn, False
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_recent_turns(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        limit: int = 6,
    ) -> list[ConversationTurnRecord]:
        if not 1 <= limit <= 50:
            raise ValueError("会话轮次limit必须位于1到50")
        connection = self._connect()
        try:
            owner_row = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ? AND owner_user_id = ?",
                (str(conversation_id), owner_user_id),
            ).fetchone()
            if owner_row is None:
                raise ConversationUnavailableError
            rows = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = ? ORDER BY sequence_number DESC LIMIT ?",
                (str(conversation_id), limit),
            ).fetchall()
            return [_turn_from_row(row) for row in reversed(rows)]
        finally:
            connection.close()

    def get_turn_for_owner(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
    ) -> ConversationTurnRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = ? AND turn_id = ? "
                "AND EXISTS (SELECT 1 FROM conversations "
                "WHERE conversations.conversation_id = conversation_turns.conversation_id "
                "AND owner_user_id = ?)",
                (str(conversation_id), str(turn_id), owner_user_id),
            ).fetchone()
            return None if row is None else _turn_from_row(row)
        finally:
            connection.close()

    def get_turn_by_workflow_thread(
        self,
        *,
        workflow_thread_id: UUID,
    ) -> ConversationTurnRecord | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE workflow_thread_id = ?",
                (str(workflow_thread_id),),
            ).fetchone()
            return None if row is None else _turn_from_row(row)
        finally:
            connection.close()

    def update_memory(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        expected_version: int,
        memory: ConversationMemory,
    ) -> ConversationRecord:
        _validate_memory_version(expected_version=expected_version, memory=memory)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._active_conversation_in_transaction(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            if existing.memory.memory_version != expected_version:
                raise ConversationVersionConflictError
            updated_at = datetime.now(UTC)
            cursor = connection.execute(
                """
                UPDATE conversations
                SET memory_json = ?, memory_version = ?, updated_at = ?
                WHERE conversation_id = ? AND owner_user_id = ? AND memory_version = ?
                """,
                (
                    memory.model_dump_json(),
                    memory.memory_version,
                    updated_at.isoformat(),
                    str(conversation_id),
                    owner_user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationVersionConflictError
            connection.commit()
            return existing.model_copy(update={"memory": memory, "updated_at": updated_at})
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def advance_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._conversation_for_turn_progress_in_transaction(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = ? AND conversation_id = ?",
                (str(turn_id), str(conversation_id)),
            ).fetchone()
            if row is None:
                raise ConversationTurnConflictError
            lease_row = connection.execute(
                "SELECT 1 FROM conversation_execution_leases WHERE turn_id = ?",
                (str(turn_id),),
            ).fetchone()
            if lease_row is not None:
                raise ConversationTurnConflictError
            updated = _apply_turn_update(
                _turn_from_row(row),
                update,
                updated_at=datetime.now(UTC),
            )
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = ?, standalone_question = ?, assistant_answer = ?, intent = ?,
                    verified_order_ids_json = ?, cited_document_ids_json = ?, updated_at = ?
                WHERE turn_id = ? AND conversation_id = ? AND status = ?
                """,
                (
                    updated.status.value,
                    updated.standalone_question,
                    updated.assistant_answer,
                    updated.intent,
                    json.dumps(updated.verified_order_ids),
                    json.dumps(updated.cited_document_ids),
                    updated.updated_at.isoformat(),
                    str(turn_id),
                    str(conversation_id),
                    update.expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationTurnConflictError
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_turn_execution(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        execution_kind: ExecutionKind,
        lease_seconds: int,
        decision_audit_event_id: str | None = None,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._conversation_for_turn_progress_in_transaction(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            turn_row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = ? AND conversation_id = ?",
                (str(turn_id), str(conversation_id)),
            ).fetchone()
            if turn_row is None:
                raise ConversationLeaseConflictError
            turn = _turn_from_row(turn_row)
            _validate_claim_source(
                turn=turn,
                execution_kind=execution_kind,
                decision_audit_event_id=decision_audit_event_id,
            )
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = ?",
                (str(turn_id),),
            ).fetchone()
            previous = None if lease_row is None else _lease_from_row(lease_row)
            if previous is not None and previous.state in {
                ExecutionLeaseState.ACTIVE,
                ExecutionLeaseState.RECONCILIATION_REQUIRED,
            }:
                raise ConversationLeaseConflictError
            now = datetime.now(UTC)
            lease = _new_execution_lease(
                turn_id=turn_id,
                kind=execution_kind,
                state=ExecutionLeaseState.ACTIVE,
                fence_generation=(
                    1 if previous is None else previous.fence_generation + 1
                ),
                now=now,
                lease_seconds=lease_seconds,
                decision_audit_event_id=decision_audit_event_id,
            )
            if execution_kind == ExecutionKind.INITIAL:
                cursor = connection.execute(
                    "UPDATE conversation_turns SET status = 'running', updated_at = ? "
                    "WHERE turn_id = ? AND conversation_id = ? AND status = 'accepted'",
                    (now.isoformat(), str(turn_id), str(conversation_id)),
                )
                if cursor.rowcount != 1:
                    raise ConversationLeaseConflictError
            connection.execute(
                """
                INSERT INTO conversation_execution_leases (
                    turn_id, kind, state, claim_token, fence_generation,
                    decision_audit_event_id, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    kind = excluded.kind,
                    state = excluded.state,
                    claim_token = excluded.claim_token,
                    fence_generation = excluded.fence_generation,
                    decision_audit_event_id = excluded.decision_audit_event_id,
                    claimed_at = excluded.claimed_at,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (
                    str(lease.turn_id),
                    lease.kind.value,
                    lease.state.value,
                    str(lease.claim_token),
                    lease.fence_generation,
                    lease.decision_audit_event_id,
                    lease.claimed_at.isoformat(),
                    lease.heartbeat_at.isoformat(),
                    lease.lease_expires_at.isoformat(),
                ),
            )
            connection.commit()
            return lease
        except (ConversationUnavailableError, ConversationTurnConflictError) as error:
            connection.rollback()
            raise ConversationLeaseConflictError from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        lease_seconds: int,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._conversation_for_turn_progress_in_transaction(
                    connection,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationTurnConflictError as error:
                raise ConversationLeaseLostError from error
            turn_row = connection.execute(
                "SELECT status FROM conversation_turns "
                "WHERE turn_id = ? AND conversation_id = ?",
                (str(lease.turn_id), str(conversation_id)),
            ).fetchone()
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = ?",
                (str(lease.turn_id),),
            ).fetchone()
            if turn_row is None or lease_row is None:
                raise ConversationLeaseLostError
            stored = _lease_from_row(lease_row)
            if not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            expected_status = (
                ConversationTurnStatus.RUNNING.value
                if stored.kind == ExecutionKind.INITIAL
                else ConversationTurnStatus.WAITING_APPROVAL.value
            )
            if turn_row["status"] != expected_status:
                raise ConversationLeaseLostError
            now = datetime.now(UTC)
            renewed = stored.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            cursor = connection.execute(
                """
                UPDATE conversation_execution_leases
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE turn_id = ? AND kind = ? AND state = 'active'
                    AND claim_token = ? AND fence_generation = ?
                """,
                (
                    renewed.heartbeat_at.isoformat(),
                    renewed.lease_expires_at.isoformat(),
                    str(lease.turn_id),
                    lease.kind.value,
                    str(lease.claim_token),
                    lease.fence_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            connection.commit()
            return renewed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._conversation_for_turn_progress_in_transaction(
                    connection,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationTurnConflictError as error:
                raise ConversationLeaseLostError from error
            turn_row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = ? AND conversation_id = ?",
                (str(lease.turn_id), str(conversation_id)),
            ).fetchone()
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = ?",
                (str(lease.turn_id),),
            ).fetchone()
            if turn_row is None or lease_row is None:
                raise ConversationLeaseLostError
            turn = _turn_from_row(turn_row)
            stored = _lease_from_row(lease_row)
            if not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            _validate_finish_source(turn=turn, lease=stored, update=update)
            now = datetime.now(UTC)
            updated = _apply_turn_update(turn, update, updated_at=now)
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = ?, standalone_question = ?, assistant_answer = ?, intent = ?,
                    verified_order_ids_json = ?, cited_document_ids_json = ?, updated_at = ?
                WHERE turn_id = ? AND conversation_id = ? AND status = ?
                """,
                (
                    updated.status.value,
                    updated.standalone_question,
                    updated.assistant_answer,
                    updated.intent,
                    json.dumps(updated.verified_order_ids),
                    json.dumps(updated.cited_document_ids),
                    updated.updated_at.isoformat(),
                    str(lease.turn_id),
                    str(conversation_id),
                    update.expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            cursor = connection.execute(
                """
                UPDATE conversation_execution_leases
                SET state = 'released', heartbeat_at = ?
                WHERE turn_id = ? AND kind = ? AND state = 'active'
                    AND claim_token = ? AND fence_generation = ?
                """,
                (
                    now.isoformat(),
                    str(lease.turn_id),
                    lease.kind.value,
                    str(lease.claim_token),
                    lease.fence_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            connection.commit()
            return updated
        except ConversationTurnConflictError as error:
            connection.rollback()
            raise ConversationLeaseLostError from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_turn_execution_lease(
        self,
        *,
        turn_id: UUID,
    ) -> ConversationExecutionLease | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = ?",
                (str(turn_id),),
            ).fetchone()
            return None if row is None else _lease_from_row(row)
        finally:
            connection.close()

    def recover_stale_turn_executions(
        self,
        *,
        now: datetime,
        grace_seconds: int,
        accepted_stale_seconds: int,
        limit: int = 100,
    ) -> ConversationExecutionRecoveryResult:
        _validate_recovery_request(
            now=now,
            grace_seconds=grace_seconds,
            accepted_stale_seconds=accepted_stale_seconds,
            limit=limit,
        )
        stable_now = now.astimezone(UTC)
        lease_cutoff = stable_now - timedelta(seconds=grace_seconds)
        legacy_cutoff = stable_now - timedelta(seconds=accepted_stale_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidate_rows = connection.execute(
                """
                SELECT turns.turn_id
                FROM conversation_turns AS turns
                JOIN conversations AS conversations
                    ON conversations.conversation_id = turns.conversation_id
                LEFT JOIN conversation_execution_leases AS leases
                    ON leases.turn_id = turns.turn_id
                WHERE conversations.status = 'active' AND (
                    (leases.state = 'active' AND leases.lease_expires_at <= ?)
                    OR (
                        turns.status IN ('accepted', 'running')
                        AND turns.updated_at <= ?
                        AND (
                            leases.turn_id IS NULL
                            OR leases.state IN ('released', 'revoked')
                        )
                    )
                )
                ORDER BY turns.updated_at, turns.turn_id
                LIMIT ?
                """,
                (lease_cutoff.isoformat(), legacy_cutoff.isoformat(), limit),
            ).fetchall()
            accepted_failed = 0
            initial_failed = 0
            approval_quarantined = 0
            legacy_manual_review = 0
            for candidate in candidate_rows:
                turn_id = UUID(candidate["turn_id"])
                turn_row = connection.execute(
                    f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns WHERE turn_id = ?",
                    (str(turn_id),),
                ).fetchone()
                assert turn_row is not None
                turn = _turn_from_row(turn_row)
                lease_row = connection.execute(
                    f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                    "WHERE turn_id = ?",
                    (str(turn_id),),
                ).fetchone()
                lease = None if lease_row is None else _lease_from_row(lease_row)
                if lease is None or lease.state != ExecutionLeaseState.ACTIVE:
                    if turn.status == ConversationTurnStatus.ACCEPTED:
                        connection.execute(
                            "UPDATE conversation_turns SET status = 'failed', updated_at = ? "
                            "WHERE turn_id = ? AND status = 'accepted'",
                            (stable_now.isoformat(), str(turn_id)),
                        )
                        accepted_failed += 1
                    else:
                        connection.execute(
                            "UPDATE conversation_turns SET updated_at = ? "
                            "WHERE turn_id = ?",
                            (stable_now.isoformat(), str(turn_id)),
                        )
                        legacy_manual_review += 1
                    continue

                recovered_state = (
                    ExecutionLeaseState.REVOKED
                    if lease.kind == ExecutionKind.INITIAL
                    else ExecutionLeaseState.RECONCILIATION_REQUIRED
                )
                recovered = _new_execution_lease(
                    turn_id=turn_id,
                    kind=lease.kind,
                    state=recovered_state,
                    fence_generation=lease.fence_generation + 1,
                    now=stable_now,
                    lease_seconds=0,
                    decision_audit_event_id=lease.decision_audit_event_id,
                )
                connection.execute(
                    """
                    UPDATE conversation_execution_leases
                    SET state = ?, claim_token = ?, fence_generation = ?,
                        claimed_at = ?, heartbeat_at = ?, lease_expires_at = ?
                    WHERE turn_id = ? AND state = 'active'
                        AND claim_token = ? AND fence_generation = ?
                    """,
                    (
                        recovered.state.value,
                        str(recovered.claim_token),
                        recovered.fence_generation,
                        recovered.claimed_at.isoformat(),
                        recovered.heartbeat_at.isoformat(),
                        recovered.lease_expires_at.isoformat(),
                        str(turn_id),
                        str(lease.claim_token),
                        lease.fence_generation,
                    ),
                )
                if (
                    lease.kind == ExecutionKind.INITIAL
                    and turn.status == ConversationTurnStatus.RUNNING
                ):
                    connection.execute(
                        "UPDATE conversation_turns SET status = 'failed', updated_at = ? "
                        "WHERE turn_id = ? AND status = 'running'",
                        (stable_now.isoformat(), str(turn_id)),
                    )
                    initial_failed += 1
                elif lease.kind == ExecutionKind.APPROVAL_RESUME:
                    approval_quarantined += 1
                else:
                    connection.execute(
                        "UPDATE conversation_turns SET updated_at = ? WHERE turn_id = ?",
                        (stable_now.isoformat(), str(turn_id)),
                    )
                    legacy_manual_review += 1
            connection.commit()
            return ConversationExecutionRecoveryResult(
                scanned_count=len(candidate_rows),
                accepted_failed_count=accepted_failed,
                initial_failed_count=initial_failed,
                approval_quarantined_count=approval_quarantined,
                legacy_manual_review_count=legacy_manual_review,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def count_conversations(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])
        finally:
            connection.close()

    def _deletion_plan_from_transaction(
        self,
        connection: sqlite3.Connection,
        record: ConversationRecord,
    ) -> ConversationDeletionPlan:
        rows = connection.execute(
            "SELECT workflow_thread_id FROM conversation_turns "
            "WHERE conversation_id = ? ORDER BY sequence_number",
            (str(record.conversation_id),),
        ).fetchall()
        return ConversationDeletionPlan(
            conversation_id=record.conversation_id,
            owner_user_id=record.owner_user_id,
            prepared_status=record.status,
            workflow_thread_ids=[UUID(row["workflow_thread_id"]) for row in rows],
        )

    def prepare_conversation_deletion(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationDeletionPlan | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE conversation_id = ? AND owner_user_id = ?",
                (str(conversation_id), owner_user_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            record = _conversation_from_row(row)
            active_row = connection.execute(
                "SELECT 1 FROM conversation_turns AS turns "
                "LEFT JOIN conversation_execution_leases AS leases "
                "ON leases.turn_id = turns.turn_id "
                "WHERE turns.conversation_id = ? AND ("
                "turns.status IN ('accepted', 'running') "
                "OR leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required')) LIMIT 1",
                (str(conversation_id),),
            ).fetchone()
            if active_row is not None:
                raise ConversationDeletionBusyError
            updated_at = datetime.now(UTC)
            connection.execute(
                "UPDATE conversations SET status = 'closed', updated_at = ? "
                "WHERE conversation_id = ? AND owner_user_id = ?",
                (updated_at.isoformat(), str(conversation_id), owner_user_id),
            )
            prepared = record.model_copy(
                update={"status": ConversationStatus.CLOSED, "updated_at": updated_at}
            )
            plan = self._deletion_plan_from_transaction(connection, prepared)
            connection.commit()
            return plan
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare_expired_conversation_deletions(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[ConversationDeletionPlan]:
        _validate_retention_request(now=now, limit=limit)
        stable_now = now.astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE (status IN ('closed', 'expired') "
                "OR (status = 'active' AND expires_at <= ?)) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM conversation_turns "
                "LEFT JOIN conversation_execution_leases AS leases "
                "ON leases.turn_id = conversation_turns.turn_id "
                "WHERE conversation_turns.conversation_id = conversations.conversation_id "
                "AND (conversation_turns.status IN ('accepted', 'running') "
                "OR leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required'))) "
                "ORDER BY updated_at, expires_at, conversation_id LIMIT ?",
                (stable_now.isoformat(), limit),
            ).fetchall()
            plans: list[ConversationDeletionPlan] = []
            for row in rows:
                record = _conversation_from_row(row)
                prepared_status = (
                    ConversationStatus.EXPIRED
                    if record.status == ConversationStatus.ACTIVE
                    else record.status
                )
                connection.execute(
                    "UPDATE conversations SET status = ?, updated_at = ? "
                    "WHERE conversation_id = ?",
                    (
                        prepared_status.value,
                        stable_now.isoformat(),
                        str(record.conversation_id),
                    ),
                )
                prepared = record.model_copy(
                    update={
                        "status": prepared_status,
                        "updated_at": stable_now,
                    }
                )
                plans.append(self._deletion_plan_from_transaction(connection, prepared))
            connection.commit()
            return plans
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_prepared_conversation(self, *, plan: ConversationDeletionPlan) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, owner_user_id FROM conversations "
                "WHERE conversation_id = ?",
                (str(plan.conversation_id),),
            ).fetchone()
            if row is None:
                # 另一个持有相同准备计划的请求已经完成物理删除，属于幂等成功。
                connection.commit()
                return True
            if (
                row["owner_user_id"] != plan.owner_user_id
                or row["status"] != plan.prepared_status.value
            ):
                connection.commit()
                return False
            turn_rows = connection.execute(
                "SELECT turn_id, workflow_thread_id, status FROM conversation_turns "
                "WHERE conversation_id = ? ORDER BY sequence_number",
                (str(plan.conversation_id),),
            ).fetchall()
            current_thread_ids = {
                UUID(turn_row["workflow_thread_id"]) for turn_row in turn_rows
            }
            blocking_lease = connection.execute(
                "SELECT 1 FROM conversation_execution_leases AS leases "
                "JOIN conversation_turns AS turns ON turns.turn_id = leases.turn_id "
                "WHERE turns.conversation_id = ? AND (leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required')) LIMIT 1",
                (str(plan.conversation_id),),
            ).fetchone()
            if (
                current_thread_ids != set(plan.workflow_thread_ids)
                or any(
                    turn_row["status"] in {"accepted", "running"}
                    for turn_row in turn_rows
                )
                or blocking_lease is not None
            ):
                connection.commit()
                return False
            connection.execute(
                "DELETE FROM conversation_execution_leases WHERE turn_id IN ("
                "SELECT turn_id FROM conversation_turns WHERE conversation_id = ?)",
                (str(plan.conversation_id),),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE conversation_id = ?",
                (str(plan.conversation_id),),
            )
            connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (str(plan.conversation_id),),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class PostgresConversationRepository:
    """使用PostgreSQL行锁、唯一键和JSONB的多实例会话仓库。"""

    def __init__(self, *, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    def create_conversation(
        self,
        *,
        owner_user_id: str,
        expires_at: datetime,
    ) -> ConversationRecord:
        now = datetime.now(UTC)
        record = ConversationRecord(
            conversation_id=uuid4(),
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
            expires_at=_normalize_expiry(expires_at),
        )
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    conversation_id, owner_user_id, status, memory_json, memory_version,
                    created_at, updated_at, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.conversation_id,
                    record.owner_user_id,
                    record.status.value,
                    Jsonb(record.memory.model_dump(mode="json")),
                    record.memory.memory_version,
                    record.created_at,
                    record.updated_at,
                    record.expires_at,
                ),
            )
        return record

    def get_conversation_for_owner(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE conversation_id = %s AND owner_user_id = %s",
                (conversation_id, owner_user_id),
            ).fetchone()
        return None if row is None else _conversation_from_row(row)

    def _locked_active_conversation(
        self,
        connection: Any,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord:
        row = connection.execute(
            f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
            "WHERE conversation_id = %s AND owner_user_id = %s FOR UPDATE",
            (conversation_id, owner_user_id),
        ).fetchone()
        if row is None:
            raise ConversationUnavailableError
        record = _conversation_from_row(row)
        _require_active_conversation(record)
        return record

    def _locked_conversation_for_turn_progress(
        self,
        connection: Any,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationRecord:
        """锁定会话，并允许活动会话中的既有轮次跨TTL完成。"""

        row = connection.execute(
            f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
            "WHERE conversation_id = %s AND owner_user_id = %s FOR UPDATE",
            (conversation_id, owner_user_id),
        ).fetchone()
        if row is None:
            raise ConversationTurnConflictError
        record = _conversation_from_row(row)
        _require_turn_progress_allowed(record)
        return record

    def create_or_get_turn(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        idempotency_key: str,
        user_message: str,
    ) -> tuple[ConversationTurnRecord, bool]:
        with self._pool.connection() as connection:
            self._locked_active_conversation(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = %s AND idempotency_key = %s",
                (conversation_id, idempotency_key),
            ).fetchone()
            if row is not None:
                existing = _turn_from_row(row)
                if existing.user_message != user_message:
                    raise ConversationIdempotencyConflictError
                return existing, True
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 AS next_sequence "
                "FROM conversation_turns WHERE conversation_id = %s",
                (conversation_id,),
            ).fetchone()
            assert sequence_row is not None
            now = datetime.now(UTC)
            turn = ConversationTurnRecord(
                turn_id=uuid4(),
                conversation_id=conversation_id,
                workflow_thread_id=uuid4(),
                sequence_number=int(sequence_row["next_sequence"]),
                idempotency_key=idempotency_key,
                user_message=user_message,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO conversation_turns (
                    turn_id, conversation_id, workflow_thread_id, sequence_number,
                    idempotency_key, status, user_message, standalone_question,
                    assistant_answer, intent, verified_order_ids_json,
                    cited_document_ids_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    turn.turn_id,
                    turn.conversation_id,
                    turn.workflow_thread_id,
                    turn.sequence_number,
                    turn.idempotency_key,
                    turn.status.value,
                    turn.user_message,
                    None,
                    None,
                    None,
                    Jsonb([]),
                    Jsonb([]),
                    turn.created_at,
                    turn.updated_at,
                ),
            )
            return turn, False

    def list_recent_turns(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        limit: int = 6,
    ) -> list[ConversationTurnRecord]:
        if not 1 <= limit <= 50:
            raise ValueError("会话轮次limit必须位于1到50")
        with self._pool.connection() as connection:
            owner_row = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = %s AND owner_user_id = %s",
                (conversation_id, owner_user_id),
            ).fetchone()
            if owner_row is None:
                raise ConversationUnavailableError
            rows = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = %s ORDER BY sequence_number DESC LIMIT %s",
                (conversation_id, limit),
            ).fetchall()
        return [_turn_from_row(row) for row in reversed(rows)]

    def get_turn_for_owner(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
    ) -> ConversationTurnRecord | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE conversation_id = %s AND turn_id = %s "
                "AND EXISTS (SELECT 1 FROM conversations "
                "WHERE conversations.conversation_id = conversation_turns.conversation_id "
                "AND owner_user_id = %s)",
                (conversation_id, turn_id, owner_user_id),
            ).fetchone()
        return None if row is None else _turn_from_row(row)

    def get_turn_by_workflow_thread(
        self,
        *,
        workflow_thread_id: UUID,
    ) -> ConversationTurnRecord | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE workflow_thread_id = %s",
                (workflow_thread_id,),
            ).fetchone()
        return None if row is None else _turn_from_row(row)

    def update_memory(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        expected_version: int,
        memory: ConversationMemory,
    ) -> ConversationRecord:
        _validate_memory_version(expected_version=expected_version, memory=memory)
        with self._pool.connection() as connection:
            existing = self._locked_active_conversation(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            if existing.memory.memory_version != expected_version:
                raise ConversationVersionConflictError
            updated_at = datetime.now(UTC)
            cursor = connection.execute(
                """
                UPDATE conversations
                SET memory_json = %s, memory_version = %s, updated_at = %s
                WHERE conversation_id = %s AND owner_user_id = %s AND memory_version = %s
                """,
                (
                    Jsonb(memory.model_dump(mode="json")),
                    memory.memory_version,
                    updated_at,
                    conversation_id,
                    owner_user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationVersionConflictError
            return existing.model_copy(update={"memory": memory, "updated_at": updated_at})

    def advance_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        with self._pool.connection() as connection:
            self._locked_conversation_for_turn_progress(
                connection,
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
            )
            row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = %s AND conversation_id = %s FOR UPDATE",
                (turn_id, conversation_id),
            ).fetchone()
            if row is None:
                raise ConversationTurnConflictError
            lease_row = connection.execute(
                "SELECT 1 FROM conversation_execution_leases "
                "WHERE turn_id = %s FOR UPDATE",
                (turn_id,),
            ).fetchone()
            if lease_row is not None:
                raise ConversationTurnConflictError
            updated = _apply_turn_update(
                _turn_from_row(row),
                update,
                updated_at=datetime.now(UTC),
            )
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = %s, standalone_question = %s, assistant_answer = %s, intent = %s,
                    verified_order_ids_json = %s, cited_document_ids_json = %s, updated_at = %s
                WHERE turn_id = %s AND conversation_id = %s AND status = %s
                """,
                (
                    updated.status.value,
                    updated.standalone_question,
                    updated.assistant_answer,
                    updated.intent,
                    Jsonb(updated.verified_order_ids),
                    Jsonb(updated.cited_document_ids),
                    updated.updated_at,
                    turn_id,
                    conversation_id,
                    update.expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationTurnConflictError
            return updated

    def claim_turn_execution(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        owner_user_id: str,
        execution_kind: ExecutionKind,
        lease_seconds: int,
        decision_audit_event_id: str | None = None,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        with self._pool.connection() as connection:
            try:
                self._locked_conversation_for_turn_progress(
                    connection,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationTurnConflictError as error:
                raise ConversationLeaseConflictError from error
            turn_row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = %s AND conversation_id = %s FOR UPDATE",
                (turn_id, conversation_id),
            ).fetchone()
            if turn_row is None:
                raise ConversationLeaseConflictError
            turn = _turn_from_row(turn_row)
            _validate_claim_source(
                turn=turn,
                execution_kind=execution_kind,
                decision_audit_event_id=decision_audit_event_id,
            )
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = %s FOR UPDATE",
                (turn_id,),
            ).fetchone()
            previous = None if lease_row is None else _lease_from_row(lease_row)
            if previous is not None and previous.state in {
                ExecutionLeaseState.ACTIVE,
                ExecutionLeaseState.RECONCILIATION_REQUIRED,
            }:
                raise ConversationLeaseConflictError
            now_row = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            assert now_row is not None
            now = now_row["now"]
            lease = _new_execution_lease(
                turn_id=turn_id,
                kind=execution_kind,
                state=ExecutionLeaseState.ACTIVE,
                fence_generation=(
                    1 if previous is None else previous.fence_generation + 1
                ),
                now=now,
                lease_seconds=lease_seconds,
                decision_audit_event_id=decision_audit_event_id,
            )
            if execution_kind == ExecutionKind.INITIAL:
                cursor = connection.execute(
                    "UPDATE conversation_turns SET status = 'running', updated_at = %s "
                    "WHERE turn_id = %s AND conversation_id = %s AND status = 'accepted'",
                    (now, turn_id, conversation_id),
                )
                if cursor.rowcount != 1:
                    raise ConversationLeaseConflictError
            connection.execute(
                """
                INSERT INTO conversation_execution_leases (
                    turn_id, kind, state, claim_token, fence_generation,
                    decision_audit_event_id, claimed_at, heartbeat_at, lease_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(turn_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    state = EXCLUDED.state,
                    claim_token = EXCLUDED.claim_token,
                    fence_generation = EXCLUDED.fence_generation,
                    decision_audit_event_id = EXCLUDED.decision_audit_event_id,
                    claimed_at = EXCLUDED.claimed_at,
                    heartbeat_at = EXCLUDED.heartbeat_at,
                    lease_expires_at = EXCLUDED.lease_expires_at
                """,
                (
                    lease.turn_id,
                    lease.kind.value,
                    lease.state.value,
                    lease.claim_token,
                    lease.fence_generation,
                    lease.decision_audit_event_id,
                    lease.claimed_at,
                    lease.heartbeat_at,
                    lease.lease_expires_at,
                ),
            )
            return lease

    def renew_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        lease_seconds: int,
    ) -> ConversationExecutionLease:
        _validate_lease_seconds(lease_seconds)
        with self._pool.connection() as connection:
            try:
                self._locked_conversation_for_turn_progress(
                    connection,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationTurnConflictError as error:
                raise ConversationLeaseLostError from error
            turn_row = connection.execute(
                "SELECT status FROM conversation_turns "
                "WHERE turn_id = %s AND conversation_id = %s FOR UPDATE",
                (lease.turn_id, conversation_id),
            ).fetchone()
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = %s FOR UPDATE",
                (lease.turn_id,),
            ).fetchone()
            if turn_row is None or lease_row is None:
                raise ConversationLeaseLostError
            stored = _lease_from_row(lease_row)
            if not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            expected_status = (
                ConversationTurnStatus.RUNNING.value
                if stored.kind == ExecutionKind.INITIAL
                else ConversationTurnStatus.WAITING_APPROVAL.value
            )
            if turn_row["status"] != expected_status:
                raise ConversationLeaseLostError
            now_row = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            assert now_row is not None
            now = now_row["now"]
            renewed = stored.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                }
            )
            cursor = connection.execute(
                """
                UPDATE conversation_execution_leases
                SET heartbeat_at = %s, lease_expires_at = %s
                WHERE turn_id = %s AND kind = %s AND state = 'active'
                    AND claim_token = %s AND fence_generation = %s
                """,
                (
                    renewed.heartbeat_at,
                    renewed.lease_expires_at,
                    lease.turn_id,
                    lease.kind.value,
                    lease.claim_token,
                    lease.fence_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            return renewed

    def finish_turn_execution(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
        lease: ConversationExecutionLease,
        update: ConversationTurnUpdate,
    ) -> ConversationTurnRecord:
        with self._pool.connection() as connection:
            try:
                self._locked_conversation_for_turn_progress(
                    connection,
                    conversation_id=conversation_id,
                    owner_user_id=owner_user_id,
                )
            except ConversationTurnConflictError as error:
                raise ConversationLeaseLostError from error
            turn_row = connection.execute(
                f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                "WHERE turn_id = %s AND conversation_id = %s FOR UPDATE",
                (lease.turn_id, conversation_id),
            ).fetchone()
            lease_row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = %s FOR UPDATE",
                (lease.turn_id,),
            ).fetchone()
            if turn_row is None or lease_row is None:
                raise ConversationLeaseLostError
            turn = _turn_from_row(turn_row)
            stored = _lease_from_row(lease_row)
            if not _lease_matches(stored, lease):
                raise ConversationLeaseLostError
            _validate_finish_source(turn=turn, lease=stored, update=update)
            now_row = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            assert now_row is not None
            now = now_row["now"]
            updated = _apply_turn_update(turn, update, updated_at=now)
            cursor = connection.execute(
                """
                UPDATE conversation_turns
                SET status = %s, standalone_question = %s, assistant_answer = %s, intent = %s,
                    verified_order_ids_json = %s, cited_document_ids_json = %s, updated_at = %s
                WHERE turn_id = %s AND conversation_id = %s AND status = %s
                """,
                (
                    updated.status.value,
                    updated.standalone_question,
                    updated.assistant_answer,
                    updated.intent,
                    Jsonb(updated.verified_order_ids),
                    Jsonb(updated.cited_document_ids),
                    updated.updated_at,
                    lease.turn_id,
                    conversation_id,
                    update.expected_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            cursor = connection.execute(
                """
                UPDATE conversation_execution_leases
                SET state = 'released', heartbeat_at = %s
                WHERE turn_id = %s AND kind = %s AND state = 'active'
                    AND claim_token = %s AND fence_generation = %s
                """,
                (
                    now,
                    lease.turn_id,
                    lease.kind.value,
                    lease.claim_token,
                    lease.fence_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise ConversationLeaseLostError
            return updated

    def get_turn_execution_lease(
        self,
        *,
        turn_id: UUID,
    ) -> ConversationExecutionLease | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                "WHERE turn_id = %s",
                (turn_id,),
            ).fetchone()
        return None if row is None else _lease_from_row(row)

    def recover_stale_turn_executions(
        self,
        *,
        now: datetime,
        grace_seconds: int,
        accepted_stale_seconds: int,
        limit: int = 100,
    ) -> ConversationExecutionRecoveryResult:
        _validate_recovery_request(
            now=now,
            grace_seconds=grace_seconds,
            accepted_stale_seconds=accepted_stale_seconds,
            limit=limit,
        )
        with self._pool.connection() as connection:
            now_row = connection.execute("SELECT clock_timestamp() AS now").fetchone()
            assert now_row is not None
            stable_now = now_row["now"]
            lease_cutoff = stable_now - timedelta(seconds=grace_seconds)
            legacy_cutoff = stable_now - timedelta(seconds=accepted_stale_seconds)
            candidate_rows = connection.execute(
                """
                SELECT turns.turn_id
                FROM conversation_turns AS turns
                JOIN conversations AS conversations
                    ON conversations.conversation_id = turns.conversation_id
                LEFT JOIN conversation_execution_leases AS leases
                    ON leases.turn_id = turns.turn_id
                WHERE conversations.status = 'active' AND (
                    (leases.state = 'active' AND leases.lease_expires_at <= %s)
                    OR (
                        turns.status IN ('accepted', 'running')
                        AND turns.updated_at <= %s
                        AND (
                            leases.turn_id IS NULL
                            OR leases.state IN ('released', 'revoked')
                        )
                    )
                )
                ORDER BY turns.updated_at, turns.turn_id
                LIMIT %s
                FOR UPDATE OF turns SKIP LOCKED
                """,
                (lease_cutoff, legacy_cutoff, limit),
            ).fetchall()
            accepted_failed = 0
            initial_failed = 0
            approval_quarantined = 0
            legacy_manual_review = 0
            for candidate in candidate_rows:
                turn_id = candidate["turn_id"]
                turn_row = connection.execute(
                    f"SELECT {TURN_SELECT_COLUMNS} FROM conversation_turns "
                    "WHERE turn_id = %s",
                    (turn_id,),
                ).fetchone()
                assert turn_row is not None
                turn = _turn_from_row(turn_row)
                lease_row = connection.execute(
                    f"SELECT {LEASE_SELECT_COLUMNS} FROM conversation_execution_leases "
                    "WHERE turn_id = %s FOR UPDATE",
                    (turn_id,),
                ).fetchone()
                lease = None if lease_row is None else _lease_from_row(lease_row)
                if lease is None or lease.state != ExecutionLeaseState.ACTIVE:
                    if turn.status == ConversationTurnStatus.ACCEPTED:
                        connection.execute(
                            "UPDATE conversation_turns SET status = 'failed', updated_at = %s "
                            "WHERE turn_id = %s AND status = 'accepted'",
                            (stable_now, turn_id),
                        )
                        accepted_failed += 1
                    else:
                        connection.execute(
                            "UPDATE conversation_turns SET updated_at = %s "
                            "WHERE turn_id = %s",
                            (stable_now, turn_id),
                        )
                        legacy_manual_review += 1
                    continue

                recovered_state = (
                    ExecutionLeaseState.REVOKED
                    if lease.kind == ExecutionKind.INITIAL
                    else ExecutionLeaseState.RECONCILIATION_REQUIRED
                )
                recovered = _new_execution_lease(
                    turn_id=turn_id,
                    kind=lease.kind,
                    state=recovered_state,
                    fence_generation=lease.fence_generation + 1,
                    now=stable_now,
                    lease_seconds=0,
                    decision_audit_event_id=lease.decision_audit_event_id,
                )
                connection.execute(
                    """
                    UPDATE conversation_execution_leases
                    SET state = %s, claim_token = %s, fence_generation = %s,
                        claimed_at = %s, heartbeat_at = %s, lease_expires_at = %s
                    WHERE turn_id = %s AND state = 'active'
                        AND claim_token = %s AND fence_generation = %s
                    """,
                    (
                        recovered.state.value,
                        recovered.claim_token,
                        recovered.fence_generation,
                        recovered.claimed_at,
                        recovered.heartbeat_at,
                        recovered.lease_expires_at,
                        turn_id,
                        lease.claim_token,
                        lease.fence_generation,
                    ),
                )
                if (
                    lease.kind == ExecutionKind.INITIAL
                    and turn.status == ConversationTurnStatus.RUNNING
                ):
                    connection.execute(
                        "UPDATE conversation_turns SET status = 'failed', updated_at = %s "
                        "WHERE turn_id = %s AND status = 'running'",
                        (stable_now, turn_id),
                    )
                    initial_failed += 1
                elif lease.kind == ExecutionKind.APPROVAL_RESUME:
                    approval_quarantined += 1
                else:
                    connection.execute(
                        "UPDATE conversation_turns SET updated_at = %s WHERE turn_id = %s",
                        (stable_now, turn_id),
                    )
                    legacy_manual_review += 1
            return ConversationExecutionRecoveryResult(
                scanned_count=len(candidate_rows),
                accepted_failed_count=accepted_failed,
                initial_failed_count=initial_failed,
                approval_quarantined_count=approval_quarantined,
                legacy_manual_review_count=legacy_manual_review,
            )

    def count_conversations(self) -> int:
        with self._pool.connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM conversations").fetchone()
        assert row is not None
        return int(row["count"])

    def _deletion_plan_from_transaction(
        self,
        connection: Any,
        record: ConversationRecord,
    ) -> ConversationDeletionPlan:
        """在会话行锁事务内拍下完整且有序的工作流线程集合。"""

        rows = connection.execute(
            "SELECT workflow_thread_id FROM conversation_turns "
            "WHERE conversation_id = %s ORDER BY sequence_number",
            (record.conversation_id,),
        ).fetchall()
        return ConversationDeletionPlan(
            conversation_id=record.conversation_id,
            owner_user_id=record.owner_user_id,
            prepared_status=record.status,
            workflow_thread_ids=[row["workflow_thread_id"] for row in rows],
        )

    def prepare_conversation_deletion(
        self,
        *,
        conversation_id: UUID,
        owner_user_id: str,
    ) -> ConversationDeletionPlan | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE conversation_id = %s AND owner_user_id = %s FOR UPDATE",
                (conversation_id, owner_user_id),
            ).fetchone()
            if row is None:
                return None
            record = _conversation_from_row(row)
            active_row = connection.execute(
                "SELECT 1 FROM conversation_turns AS turns "
                "LEFT JOIN conversation_execution_leases AS leases "
                "ON leases.turn_id = turns.turn_id "
                "WHERE turns.conversation_id = %s AND ("
                "turns.status IN ('accepted', 'running') "
                "OR leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required')) LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active_row is not None:
                raise ConversationDeletionBusyError
            updated_at = datetime.now(UTC)
            connection.execute(
                "UPDATE conversations SET status = 'closed', updated_at = %s "
                "WHERE conversation_id = %s AND owner_user_id = %s",
                (updated_at, conversation_id, owner_user_id),
            )
            prepared = record.model_copy(
                update={"status": ConversationStatus.CLOSED, "updated_at": updated_at}
            )
            return self._deletion_plan_from_transaction(connection, prepared)

    def prepare_expired_conversation_deletions(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[ConversationDeletionPlan]:
        _validate_retention_request(now=now, limit=limit)
        stable_now = now.astimezone(UTC)
        with self._pool.connection() as connection:
            rows = connection.execute(
                f"SELECT {CONVERSATION_SELECT_COLUMNS} FROM conversations "
                "WHERE (status IN ('closed', 'expired') "
                "OR (status = 'active' AND expires_at <= %s)) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM conversation_turns "
                "LEFT JOIN conversation_execution_leases AS leases "
                "ON leases.turn_id = conversation_turns.turn_id "
                "WHERE conversation_turns.conversation_id = conversations.conversation_id "
                "AND (conversation_turns.status IN ('accepted', 'running') "
                "OR leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required'))) "
                "ORDER BY updated_at, expires_at, conversation_id "
                "LIMIT %s FOR UPDATE SKIP LOCKED",
                (stable_now, limit),
            ).fetchall()
            plans: list[ConversationDeletionPlan] = []
            for row in rows:
                record = _conversation_from_row(row)
                # SELECT 的 READ COMMITTED 快照可能早于一个刚提交的新轮次；取得会话行锁后
                # 必须用新语句再次检查。此后 create_or_get_turn 会阻塞在同一会话行上。
                active_row = connection.execute(
                    "SELECT 1 FROM conversation_turns AS turns "
                    "LEFT JOIN conversation_execution_leases AS leases "
                    "ON leases.turn_id = turns.turn_id "
                    "WHERE turns.conversation_id = %s AND ("
                    "turns.status IN ('accepted', 'running') "
                    "OR leases.state = 'active' "
                    "OR (leases.kind = 'approval_resume' "
                    "AND leases.state = 'reconciliation_required')) LIMIT 1",
                    (record.conversation_id,),
                ).fetchone()
                if active_row is not None:
                    continue
                prepared_status = (
                    ConversationStatus.EXPIRED
                    if record.status == ConversationStatus.ACTIVE
                    else record.status
                )
                connection.execute(
                    "UPDATE conversations SET status = %s, updated_at = %s "
                    "WHERE conversation_id = %s",
                    (prepared_status.value, stable_now, record.conversation_id),
                )
                prepared = record.model_copy(
                    update={"status": prepared_status, "updated_at": stable_now}
                )
                plans.append(self._deletion_plan_from_transaction(connection, prepared))
            return plans

    def delete_prepared_conversation(self, *, plan: ConversationDeletionPlan) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT status, owner_user_id FROM conversations "
                "WHERE conversation_id = %s FOR UPDATE",
                (plan.conversation_id,),
            ).fetchone()
            if row is None:
                # 另一个持有相同准备计划的请求已经完成物理删除，属于幂等成功。
                return True
            if (
                row["owner_user_id"] != plan.owner_user_id
                or row["status"] != plan.prepared_status.value
            ):
                return False
            turn_rows = connection.execute(
                "SELECT turn_id, workflow_thread_id, status FROM conversation_turns "
                "WHERE conversation_id = %s ORDER BY sequence_number",
                (plan.conversation_id,),
            ).fetchall()
            current_thread_ids = {row["workflow_thread_id"] for row in turn_rows}
            blocking_lease = connection.execute(
                "SELECT 1 FROM conversation_execution_leases AS leases "
                "JOIN conversation_turns AS turns ON turns.turn_id = leases.turn_id "
                "WHERE turns.conversation_id = %s AND (leases.state = 'active' "
                "OR (leases.kind = 'approval_resume' "
                "AND leases.state = 'reconciliation_required')) LIMIT 1",
                (plan.conversation_id,),
            ).fetchone()
            if (
                current_thread_ids != set(plan.workflow_thread_ids)
                or any(
                    row["status"] in {"accepted", "running"} for row in turn_rows
                )
                or blocking_lease is not None
            ):
                return False
            connection.execute(
                "DELETE FROM conversation_execution_leases WHERE turn_id IN ("
                "SELECT turn_id FROM conversation_turns WHERE conversation_id = %s)",
                (plan.conversation_id,),
            )
            connection.execute(
                "DELETE FROM conversation_turns WHERE conversation_id = %s",
                (plan.conversation_id,),
            )
            cursor = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = %s "
                "AND owner_user_id = %s AND status = %s",
                (
                    plan.conversation_id,
                    plan.owner_user_id,
                    plan.prepared_status.value,
                ),
            )
            return cursor.rowcount == 1
