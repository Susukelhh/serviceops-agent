"""内存与SQLite会话仓库的所有权、幂等、版本和恢复测试。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from time import sleep
from uuid import UUID, uuid4

import pytest

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
    ConversationIdempotencyConflictError,
    ConversationLeaseConflictError,
    ConversationLeaseLostError,
    ConversationRepository,
    ConversationTurnConflictError,
    ConversationVersionConflictError,
    InMemoryConversationRepository,
    SQLiteConversationRepository,
)


def _expires_later() -> datetime:
    """为测试创建明确UTC会话期限。"""

    return datetime.now(UTC) + timedelta(days=7)


@pytest.fixture(params=["memory", "sqlite"])
def repository(request: pytest.FixtureRequest, tmp_path: Path) -> ConversationRepository:
    """让同一组契约测试覆盖两种无需外部服务的实现。"""

    if request.param == "memory":
        return InMemoryConversationRepository()
    return SQLiteConversationRepository(database_path=tmp_path / "conversations.sqlite3")


def test_conversation_owner_boundary_hides_other_users(
    repository: ConversationRepository,
) -> None:
    """会话标识不是授权凭证，越权读取必须与不存在表现相同。"""

    created = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )

    assert repository.get_conversation_for_owner(
        conversation_id=created.conversation_id,
        owner_user_id="user-001",
    ) == created
    assert (
        repository.get_conversation_for_owner(
            conversation_id=created.conversation_id,
            owner_user_id="user-002",
        )
        is None
    )


def test_turn_creation_is_idempotent_and_allocates_monotonic_sequence(
    repository: ConversationRepository,
) -> None:
    """同键重试返回原轮次，不同消息获得递增序号和独立工作流线程。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    first, first_replayed = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="turn-key-0001",
        user_message="SO100001能退货吗？",
    )
    replay, replayed = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="turn-key-0001",
        user_message="SO100001能退货吗？",
    )
    second, second_replayed = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="turn-key-0002",
        user_message="那运费谁承担？",
    )

    assert first_replayed is False
    assert replayed is True
    assert second_replayed is False
    assert replay.turn_id == first.turn_id
    assert [first.sequence_number, second.sequence_number] == [1, 2]
    assert first.workflow_thread_id != second.workflow_thread_id
    assert repository.get_turn_by_workflow_thread(
        workflow_thread_id=first.workflow_thread_id,
    ) == first
    assert (
        repository.get_turn_by_workflow_thread(workflow_thread_id=uuid4()) is None
    )

    with pytest.raises(ConversationIdempotencyConflictError):
        repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id="user-001",
            idempotency_key="turn-key-0001",
            user_message="同一个键却换了问题",
        )


def test_memory_update_uses_optimistic_version(
    repository: ConversationRepository,
) -> None:
    """并发请求不能用旧快照静默覆盖更新后的会话槽位。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    updated = repository.update_memory(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        expected_version=0,
        memory=ConversationMemory(
            memory_version=1,
            current_topic="return_policy",
            active_order_id="SO100001",
        ),
    )

    assert updated.memory.memory_version == 1
    assert updated.memory.active_order_id == "SO100001"

    with pytest.raises(ConversationVersionConflictError):
        repository.update_memory(
            conversation_id=conversation.conversation_id,
            owner_user_id="user-001",
            expected_version=0,
            memory=ConversationMemory(memory_version=1, current_topic="shipping"),
        )


def test_turn_state_advances_once_and_persists_safe_outcome(
    repository: ConversationRepository,
) -> None:
    """轮次只能按有限状态机前进，完成结果保存已验证订单和引用文档。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="turn-state-0001",
        user_message="那运费呢？",
    )
    running = repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
            standalone_question="SO100001退货时运费由谁承担？",
            verified_order_ids=["SO100001"],
        ),
    )
    completed = repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            standalone_question=running.standalone_question,
            assistant_answer="退货运费按当前已发布政策处理。",
            intent="faq",
            verified_order_ids=["SO100001"],
            cited_document_ids=["KB-RETURN-001"],
        ),
    )

    assert completed.status == ConversationTurnStatus.COMPLETED
    assert completed.verified_order_ids == ["SO100001"]
    assert completed.cited_document_ids == ["KB-RETURN-001"]

    with pytest.raises(ConversationTurnConflictError):
        repository.advance_turn(
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            owner_user_id="user-001",
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.RUNNING,
                status=ConversationTurnStatus.FAILED,
            ),
        )


def test_waiting_approval_turn_can_be_claimed_by_only_one_request(
    repository: ConversationRepository,
) -> None:
    """两个审批请求并发认领同一轮时，只有一个能进入running。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="approval-claim-0001",
        user_message="申请退货",
    )
    running = repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )
    waiting = repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.WAITING_APPROVAL,
            standalone_question="为订单 SO100002 申请退货",
            assistant_answer="等待审批",
            intent="return_request",
            verified_order_ids=["SO100002"],
        ),
    )

    def claim() -> bool:
        try:
            repository.advance_turn(
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                owner_user_id="user-001",
                update=ConversationTurnUpdate(
                    expected_status=ConversationTurnStatus.WAITING_APPROVAL,
                    status=ConversationTurnStatus.RUNNING,
                    standalone_question=waiting.standalone_question,
                    assistant_answer=waiting.assistant_answer,
                    intent=waiting.intent,
                    verified_order_ids=waiting.verified_order_ids,
                ),
            )
            return True
        except ConversationTurnConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: claim(), range(2)))

    assert running.status == ConversationTurnStatus.RUNNING
    assert sorted(outcomes) == [False, True]


def test_sqlite_conversation_and_turns_survive_repository_restart(tmp_path: Path) -> None:
    """本地进程重启后仍能读取会话所有权、记忆和有序轮次。"""

    database_path = tmp_path / "restart-conversations.sqlite3"
    first_repository = SQLiteConversationRepository(database_path=database_path)
    conversation = first_repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    first_repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="restart-turn-0001",
        user_message="查询SO100001",
    )
    first_repository.update_memory(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        expected_version=0,
        memory=ConversationMemory(memory_version=1, active_order_id="SO100001"),
    )

    restarted_repository = SQLiteConversationRepository(database_path=database_path)
    restored = restarted_repository.get_conversation_for_owner(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
    )
    turns = restarted_repository.list_recent_turns(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
    )

    assert restored is not None
    assert restored.memory.active_order_id == "SO100001"
    assert [turn.sequence_number for turn in turns] == [1]


def test_sqlite_execution_lease_survives_repository_restart(tmp_path: Path) -> None:
    """进程重启不能丢失活动token或把同一轮的generation重新从1开始。"""

    database_path = tmp_path / "restart-execution-lease.sqlite3"
    first = SQLiteConversationRepository(database_path=database_path)
    owner = "restart-lease-owner"
    conversation_id, turn_id = _new_turn_for_execution(first, owner_user_id=owner)
    claimed = first.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=30,
    )

    restarted = SQLiteConversationRepository(database_path=database_path)
    restored = restarted.get_turn_execution_lease(turn_id=turn_id)

    assert restored == claimed
    completed = restarted.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=claimed,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="重启后完成。",
        ),
    )
    assert completed.status == ConversationTurnStatus.COMPLETED


def test_sqlite_serializes_concurrent_same_idempotency_key(tmp_path: Path) -> None:
    """两个SQLite仓库并发创建同一轮次时只能分配一个序号和工作流线程。"""

    database_path = tmp_path / "concurrent-conversations.sqlite3"
    first_repository = SQLiteConversationRepository(database_path=database_path)
    second_repository = SQLiteConversationRepository(database_path=database_path)
    conversation = first_repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )

    def create_turn(repository: SQLiteConversationRepository) -> tuple[str, bool]:
        turn, replayed = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id="user-001",
            idempotency_key="concurrent-turn-0001",
            user_message="并发提交同一轮",
        )
        return str(turn.turn_id), replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_turn, (first_repository, second_repository)))

    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    turns = first_repository.list_recent_turns(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
    )
    assert len(turns) == 1


def test_unknown_turn_cannot_be_advanced(repository: ConversationRepository) -> None:
    """随机轮次ID不能借用合法会话越权推进状态。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    with pytest.raises(ConversationTurnConflictError):
        repository.advance_turn(
            conversation_id=conversation.conversation_id,
            turn_id=uuid4(),
            owner_user_id="user-001",
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.ACCEPTED,
                status=ConversationTurnStatus.FAILED,
            ),
        )


def _new_turn_for_execution(
    repository: ConversationRepository,
    *,
    owner_user_id: str = "lease-owner-001",
) -> tuple[UUID, UUID]:
    """创建可由租约接口原子认领的一轮。"""

    conversation = repository.create_conversation(
        owner_user_id=owner_user_id,
        expires_at=_expires_later(),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id=owner_user_id,
        idempotency_key=f"lease-turn-{uuid4().hex}",
        user_message="执行租约测试",
    )
    return conversation.conversation_id, turn.turn_id


def test_initial_execution_lease_claim_renew_finish_and_fence(
    repository: ConversationRepository,
) -> None:
    """初始认领与RUNNING转换原子完成，后续写入只能使用活动fence。"""

    owner = "lease-owner-001"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=30,
    )

    assert lease.state == ExecutionLeaseState.ACTIVE
    assert lease.fence_generation == 1
    assert repository.get_turn_execution_lease(turn_id=turn_id) == lease
    with pytest.raises(ConversationLeaseConflictError):
        repository.claim_turn_execution(
            conversation_id=conversation_id,
            turn_id=turn_id,
            owner_user_id=owner,
            execution_kind=ExecutionKind.INITIAL,
            lease_seconds=30,
        )
    with pytest.raises(ConversationTurnConflictError):
        repository.advance_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            owner_user_id=owner,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.RUNNING,
                status=ConversationTurnStatus.COMPLETED,
                assistant_answer="不能绕过租约。",
            ),
        )

    renewed = repository.renew_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=lease,
        lease_seconds=60,
    )
    assert renewed.claim_token == lease.claim_token
    assert renewed.fence_generation == lease.fence_generation
    assert renewed.lease_expires_at > lease.lease_expires_at

    completed = repository.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=renewed,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="执行完成。",
        ),
    )
    assert completed.status == ConversationTurnStatus.COMPLETED
    stored = repository.get_turn_execution_lease(turn_id=turn_id)
    assert stored is not None
    assert stored.state == ExecutionLeaseState.RELEASED
    with pytest.raises(ConversationLeaseLostError):
        repository.renew_turn_execution(
            conversation_id=conversation_id,
            owner_user_id=owner,
            lease=renewed,
            lease_seconds=30,
        )


def test_only_one_concurrent_initial_execution_claim_wins(
    repository: ConversationRepository,
) -> None:
    """两个worker同时领取同一轮时只能有一个获得第一代token。"""

    owner = "concurrent-lease-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )

    def claim() -> bool:
        try:
            repository.claim_turn_execution(
                conversation_id=conversation_id,
                turn_id=turn_id,
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
    stored = repository.get_turn_execution_lease(turn_id=turn_id)
    assert stored is not None
    assert stored.fence_generation == 1


def test_expired_initial_lease_recovery_fences_late_worker(
    repository: ConversationRepository,
) -> None:
    """恢复任务递增generation并关闭初始执行，旧worker再晚也不能覆盖结果。"""

    owner = "initial-recovery-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=1,
    )

    result = repository.recover_stale_turn_executions(
        now=lease.lease_expires_at + timedelta(seconds=1),
        grace_seconds=0,
        accepted_stale_seconds=60,
    )

    assert result.scanned_count == 1
    assert result.initial_failed_count == 1
    recovered = repository.get_turn_execution_lease(turn_id=turn_id)
    assert recovered is not None
    assert recovered.state == ExecutionLeaseState.REVOKED
    assert recovered.fence_generation == lease.fence_generation + 1
    assert recovered.claim_token != lease.claim_token
    with pytest.raises(ConversationLeaseLostError):
        repository.finish_turn_execution(
            conversation_id=conversation_id,
            owner_user_id=owner,
            lease=lease,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.RUNNING,
                status=ConversationTurnStatus.COMPLETED,
                assistant_answer="迟到结果不能提交。",
            ),
        )


def test_just_expired_lease_can_finish_before_recovery_fences_it(
    repository: ConversationRepository,
) -> None:
    """到期只是恢复候选条件；新代次尚未写入时原持有者仍可原子收尾。"""

    owner = "expiry-race-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=1,
    )
    sleep(1.05)

    completed = repository.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=lease,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="在恢复任务fence前完成。",
        ),
    )

    assert completed.status == ConversationTurnStatus.COMPLETED
    stored = repository.get_turn_execution_lease(turn_id=turn_id)
    assert stored is not None
    assert stored.state == ExecutionLeaseState.RELEASED


def test_stale_accepted_turn_without_lease_is_failed(
    repository: ConversationRepository,
) -> None:
    """进程在claim前崩溃留下的旧ACCEPTED轮次可被确定性关闭。"""

    owner = "accepted-recovery-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )

    result = repository.recover_stale_turn_executions(
        now=datetime.now(UTC) + timedelta(minutes=5),
        grace_seconds=0,
        accepted_stale_seconds=60,
    )

    assert result.scanned_count == 1
    assert result.accepted_failed_count == 1
    with pytest.raises(ConversationLeaseConflictError):
        repository.claim_turn_execution(
            conversation_id=conversation_id,
            turn_id=turn_id,
            owner_user_id=owner,
            execution_kind=ExecutionKind.INITIAL,
            lease_seconds=30,
        )


def test_legacy_manual_review_candidates_rotate_between_recovery_batches(
    repository: ConversationRepository,
) -> None:
    """遗留RUNNING项不能在小批次中永久占住队首、饿死后续人工处置项。"""

    owner = "legacy-recovery-owner"
    conversation = repository.create_conversation(
        owner_user_id=owner,
        expires_at=_expires_later(),
    )
    turns = []
    for index in range(2):
        turn, _ = repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id=owner,
            idempotency_key=f"legacy-running-{index:04d}",
            user_message=f"遗留执行{index}",
        )
        turns.append(
            repository.advance_turn(
                conversation_id=conversation.conversation_id,
                turn_id=turn.turn_id,
                owner_user_id=owner,
                update=ConversationTurnUpdate(
                    expected_status=ConversationTurnStatus.ACCEPTED,
                    status=ConversationTurnStatus.RUNNING,
                ),
            )
        )

    first_now = datetime.now(UTC) + timedelta(minutes=5)
    first = repository.recover_stale_turn_executions(
        now=first_now,
        grace_seconds=0,
        accepted_stale_seconds=60,
        limit=1,
    )
    second = repository.recover_stale_turn_executions(
        now=first_now + timedelta(seconds=1),
        grace_seconds=0,
        accepted_stale_seconds=60,
        limit=1,
    )
    restored = [
        repository.get_turn_by_workflow_thread(
            workflow_thread_id=turn.workflow_thread_id,
        )
        for turn in turns
    ]

    assert first.legacy_manual_review_count == 1
    assert second.legacy_manual_review_count == 1
    assert all(turn is not None and turn.updated_at >= first_now for turn in restored)


def test_approval_lease_recovery_requires_manual_reconciliation(
    repository: ConversationRepository,
) -> None:
    """审批恢复超时只fence并隔离，不自动resume也不把WAITING误写为失败。"""

    owner = "approval-recovery-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    repository.advance_turn(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )
    repository.advance_turn(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.WAITING_APPROVAL,
            assistant_answer="等待审批。",
        ),
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.APPROVAL_RESUME,
        lease_seconds=1,
        decision_audit_event_id=str(uuid4()),
    )

    result = repository.recover_stale_turn_executions(
        now=lease.lease_expires_at + timedelta(seconds=1),
        grace_seconds=0,
        accepted_stale_seconds=60,
    )

    assert result.approval_quarantined_count == 1
    recovered = repository.get_turn_execution_lease(turn_id=turn_id)
    assert recovered is not None
    assert recovered.state == ExecutionLeaseState.RECONCILIATION_REQUIRED
    assert recovered.fence_generation == lease.fence_generation + 1
    with pytest.raises(ConversationLeaseLostError):
        repository.finish_turn_execution(
            conversation_id=conversation_id,
            owner_user_id=owner,
            lease=lease,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.WAITING_APPROVAL,
                status=ConversationTurnStatus.COMPLETED,
                assistant_answer="迟到审批结果。",
            ),
        )
    with pytest.raises(ConversationDeletionBusyError):
        repository.prepare_conversation_deletion(
            conversation_id=conversation_id,
            owner_user_id=owner,
        )


def test_initial_and_approval_leases_share_monotonic_generation(
    repository: ConversationRepository,
) -> None:
    """同一轮从初始执行转入审批恢复时复用租约行并严格递增代次。"""

    owner = "two-stage-lease-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    initial = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=30,
    )
    waiting = repository.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=initial,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.WAITING_APPROVAL,
            assistant_answer="等待审批。",
        ),
    )
    assert waiting.status == ConversationTurnStatus.WAITING_APPROVAL

    approval = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.APPROVAL_RESUME,
        lease_seconds=30,
        decision_audit_event_id=str(uuid4()),
    )
    assert approval.fence_generation == initial.fence_generation + 1
    assert approval.claim_token != initial.claim_token
    with pytest.raises(ConversationLeaseConflictError):
        repository.finish_turn_execution(
            conversation_id=conversation_id,
            owner_user_id=owner,
            lease=approval,
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.WAITING_APPROVAL,
                status=ConversationTurnStatus.RUNNING,
            ),
        )
    completed = repository.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=approval,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.WAITING_APPROVAL,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="审批恢复完成。",
        ),
    )
    assert completed.status == ConversationTurnStatus.COMPLETED


def test_prepared_deletion_removes_released_execution_lease(
    repository: ConversationRepository,
) -> None:
    """业务轮次物理删除前先删租约，不能留下孤立的fence元数据。"""

    owner = "lease-delete-owner"
    conversation_id, turn_id = _new_turn_for_execution(
        repository,
        owner_user_id=owner,
    )
    lease = repository.claim_turn_execution(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner,
        execution_kind=ExecutionKind.INITIAL,
        lease_seconds=30,
    )
    repository.finish_turn_execution(
        conversation_id=conversation_id,
        owner_user_id=owner,
        lease=lease,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="可删除。",
        ),
    )
    plan = repository.prepare_conversation_deletion(
        conversation_id=conversation_id,
        owner_user_id=owner,
    )
    assert plan is not None
    assert repository.delete_prepared_conversation(plan=plan) is True
    assert repository.get_turn_execution_lease(turn_id=turn_id) is None


def _complete_turn(
    repository: ConversationRepository,
    *,
    conversation_id: UUID,
    turn_id: UUID,
    owner_user_id: str = "user-001",
) -> None:
    """把契约测试中的轮次推进到可安全清理的不可变终态。"""

    repository.advance_turn(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner_user_id,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )
    repository.advance_turn(
        conversation_id=conversation_id,
        turn_id=turn_id,
        owner_user_id=owner_user_id,
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.COMPLETED,
            assistant_answer="已完成。",
        ),
    )


def test_prepared_deletion_removes_all_turns_and_indexes(
    repository: ConversationRepository,
) -> None:
    """先关闭、再删Checkpoint、最后物理删除的计划必须覆盖全部轮次映射。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    turns = [
        repository.create_or_get_turn(
            conversation_id=conversation.conversation_id,
            owner_user_id="user-001",
            idempotency_key=f"delete-turn-{index:04d}",
            user_message=f"第{index}轮",
        )[0]
        for index in range(1, 4)
    ]
    for turn in turns:
        _complete_turn(
            repository,
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
        )

    assert (
        repository.prepare_conversation_deletion(
            conversation_id=conversation.conversation_id,
            owner_user_id="different-user",
        )
        is None
    )
    plan = repository.prepare_conversation_deletion(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
    )

    assert plan is not None
    assert plan.prepared_status == ConversationStatus.CLOSED
    assert plan.workflow_thread_ids == [turn.workflow_thread_id for turn in turns]
    assert repository.delete_prepared_conversation(plan=plan) is True
    assert repository.count_conversations() == 0
    assert repository.delete_prepared_conversation(plan=plan) is True
    for turn in turns:
        assert (
            repository.get_turn_by_workflow_thread(
                workflow_thread_id=turn.workflow_thread_id,
            )
            is None
        )


def test_deletion_refuses_running_turn_and_allows_waiting_approval(
    repository: ConversationRepository,
) -> None:
    """运行中的图不能被删Checkpoint；尚未写业务的审批中断可以被关闭。"""

    running_conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    running_turn, _ = repository.create_or_get_turn(
        conversation_id=running_conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="running-delete-0001",
        user_message="正在执行",
    )
    repository.advance_turn(
        conversation_id=running_conversation.conversation_id,
        turn_id=running_turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )

    with pytest.raises(ConversationDeletionBusyError):
        repository.prepare_conversation_deletion(
            conversation_id=running_conversation.conversation_id,
            owner_user_id="user-001",
        )

    waiting_conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    waiting_turn, _ = repository.create_or_get_turn(
        conversation_id=waiting_conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="waiting-delete-0001",
        user_message="申请退货",
    )
    repository.advance_turn(
        conversation_id=waiting_conversation.conversation_id,
        turn_id=waiting_turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )
    repository.advance_turn(
        conversation_id=waiting_conversation.conversation_id,
        turn_id=waiting_turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.WAITING_APPROVAL,
            assistant_answer="等待审批。",
        ),
    )

    waiting_plan = repository.prepare_conversation_deletion(
        conversation_id=waiting_conversation.conversation_id,
        owner_user_id="user-001",
    )
    assert waiting_plan is not None
    assert waiting_plan.workflow_thread_ids == [waiting_turn.workflow_thread_id]


def test_physical_deletion_rejects_incomplete_thread_plan(
    repository: ConversationRepository,
) -> None:
    """遗漏任何线程ID的伪造计划都不能让业务表先丢失Checkpoint映射。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="forged-plan-0001",
        user_message="保留映射",
    )
    _complete_turn(
        repository,
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
    )
    actual_plan = repository.prepare_conversation_deletion(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
    )
    assert actual_plan is not None
    incomplete_plan = ConversationDeletionPlan(
        conversation_id=actual_plan.conversation_id,
        owner_user_id=actual_plan.owner_user_id,
        prepared_status=actual_plan.prepared_status,
        workflow_thread_ids=[],
    )

    assert repository.delete_prepared_conversation(plan=incomplete_plan) is False
    assert repository.get_turn_by_workflow_thread(
        workflow_thread_id=turn.workflow_thread_id,
    ) is not None
    assert repository.delete_prepared_conversation(plan=actual_plan) is True


def test_retention_batch_retries_closed_and_expired_but_skips_running(
    repository: ConversationRepository,
) -> None:
    """清理批次可补偿失败计划，同时不会领取仍有活动工作流的会话。"""

    expiry = datetime.now(UTC) + timedelta(hours=1)
    expired = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=expiry,
    )
    expired_turn, _ = repository.create_or_get_turn(
        conversation_id=expired.conversation_id,
        owner_user_id="user-001",
        idempotency_key="expired-cleanup-0001",
        user_message="可清理",
    )
    _complete_turn(
        repository,
        conversation_id=expired.conversation_id,
        turn_id=expired_turn.turn_id,
    )

    closed = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=_expires_later(),
    )
    closed_plan = repository.prepare_conversation_deletion(
        conversation_id=closed.conversation_id,
        owner_user_id="user-001",
    )
    assert closed_plan is not None

    busy = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=expiry,
    )
    busy_turn, _ = repository.create_or_get_turn(
        conversation_id=busy.conversation_id,
        owner_user_id="user-001",
        idempotency_key="busy-cleanup-0001",
        user_message="仍在运行",
    )
    repository.advance_turn(
        conversation_id=busy.conversation_id,
        turn_id=busy_turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )

    plans = repository.prepare_expired_conversation_deletions(
        now=expiry + timedelta(hours=1),
    )
    plans_by_id = {plan.conversation_id: plan for plan in plans}

    assert expired.conversation_id in plans_by_id
    assert plans_by_id[expired.conversation_id].prepared_status == ConversationStatus.EXPIRED
    assert closed.conversation_id in plans_by_id
    assert plans_by_id[closed.conversation_id].prepared_status == ConversationStatus.CLOSED
    assert busy.conversation_id not in plans_by_id


def test_failed_retention_plans_rotate_behind_new_candidates(
    repository: ConversationRepository,
) -> None:
    """一个批次持续失败时，updated_at轮转仍让后续到期会话获得清理机会。"""

    expiry = datetime.now(UTC) + timedelta(hours=1)
    conversations = [
        repository.create_conversation(
            owner_user_id=f"rotation-owner-{index:03d}",
            expires_at=expiry,
        )
        for index in range(4)
    ]
    first_batch = repository.prepare_expired_conversation_deletions(
        now=expiry + timedelta(hours=1),
        limit=2,
    )
    # 模拟两个计划的Checkpoint均失败：不调用物理删除，直接开始下一批。
    second_batch = repository.prepare_expired_conversation_deletions(
        now=expiry + timedelta(hours=1, seconds=1),
        limit=2,
    )

    first_ids = {plan.conversation_id for plan in first_batch}
    second_ids = {plan.conversation_id for plan in second_batch}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {
        conversation.conversation_id for conversation in conversations
    }


@pytest.mark.parametrize(
    "prepared_status",
    [ConversationStatus.CLOSED, ConversationStatus.EXPIRED],
)
def test_prepared_conversation_cannot_resume_waiting_turn(
    repository: ConversationRepository,
    prepared_status: ConversationStatus,
) -> None:
    """封存状态是删除栅栏，审批暂停轮次也不能在其后重新进入执行。"""

    expiry = datetime.now(UTC) + timedelta(hours=1)
    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=expiry,
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key=f"sealed-turn-{prepared_status.value}",
        user_message="等待审批后测试封存",
    )
    repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.ACCEPTED,
            status=ConversationTurnStatus.RUNNING,
        ),
    )
    waiting = repository.advance_turn(
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id="user-001",
        update=ConversationTurnUpdate(
            expected_status=ConversationTurnStatus.RUNNING,
            status=ConversationTurnStatus.WAITING_APPROVAL,
            assistant_answer="等待审批。",
        ),
    )

    if prepared_status == ConversationStatus.CLOSED:
        plan = repository.prepare_conversation_deletion(
            conversation_id=conversation.conversation_id,
            owner_user_id="user-001",
        )
        assert plan is not None
    else:
        plans = repository.prepare_expired_conversation_deletions(
            now=expiry + timedelta(hours=1),
        )
        plan = next(
            item for item in plans if item.conversation_id == conversation.conversation_id
        )
    assert plan.prepared_status == prepared_status

    with pytest.raises(ConversationTurnConflictError):
        repository.advance_turn(
            conversation_id=conversation.conversation_id,
            turn_id=turn.turn_id,
            owner_user_id="user-001",
            update=ConversationTurnUpdate(
                expected_status=ConversationTurnStatus.WAITING_APPROVAL,
                status=ConversationTurnStatus.RUNNING,
                assistant_answer=waiting.assistant_answer,
            ),
        )


def test_non_utc_expiry_is_normalized_before_retention_comparison(
    repository: ConversationRepository,
) -> None:
    """SQLite期限文本必须统一为UTC，不能让时区偏移破坏TTL排序和比较。"""

    china_standard_time = timezone(timedelta(hours=8))
    source_expiry = (datetime.now(UTC) + timedelta(hours=1)).astimezone(
        china_standard_time
    )
    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=source_expiry,
    )

    assert conversation.expires_at.utcoffset() == timedelta(0)
    plans = repository.prepare_expired_conversation_deletions(
        now=source_expiry.astimezone(UTC) + timedelta(seconds=1),
    )
    plan = next(
        item for item in plans if item.conversation_id == conversation.conversation_id
    )
    assert plan.prepared_status == ConversationStatus.EXPIRED


def test_accepted_turn_can_finish_after_ttl_boundary(
    repository: ConversationRepository,
) -> None:
    """TTL只截断新轮次；到期前已接纳的工作流仍能安全收尾。"""

    conversation = repository.create_conversation(
        owner_user_id="user-001",
        expires_at=datetime.now(UTC) + timedelta(milliseconds=250),
    )
    turn, _ = repository.create_or_get_turn(
        conversation_id=conversation.conversation_id,
        owner_user_id="user-001",
        idempotency_key="cross-ttl-0001",
        user_message="即将跨过TTL",
    )
    sleep(0.3)

    _complete_turn(
        repository,
        conversation_id=conversation.conversation_id,
        turn_id=turn.turn_id,
    )

    restored = repository.get_turn_by_workflow_thread(
        workflow_thread_id=turn.workflow_thread_id,
    )
    assert restored is not None
    assert restored.status == ConversationTurnStatus.COMPLETED


def test_conversation_migration_extends_existing_revision_chain() -> None:
    """会话表必须通过新revision追加，不能改写已经发布的初始迁移。"""

    migration = import_module(
        "serviceops_agent.migrations.versions.20260829_0002_conversation_memory"
    )

    assert migration.revision == "20260829_0002"
    assert migration.down_revision == "20260821_0001"
