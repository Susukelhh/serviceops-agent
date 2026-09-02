"""反馈问题池的内存、SQLite 和 PostgreSQL 仓库。"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol
from uuid import UUID, uuid4

from serviceops_agent.domain.conversation import ConversationTurnRecord
from serviceops_agent.domain.feedback import (
    FeedbackCategory,
    FeedbackReason,
    FeedbackRecord,
    FeedbackReview,
    FeedbackSignal,
    FeedbackStatus,
    KnowledgeCandidate,
)
from serviceops_agent.infrastructure.postgres_repository import PostgresConnectionPool


class FeedbackRepositoryError(Exception):
    """反馈仓库的可预期冲突。"""


class FeedbackConflictError(FeedbackRepositoryError):
    """同一幂等键被用于不同反馈，或反馈已经用另一决定处置。"""


class FeedbackNotFoundError(FeedbackRepositoryError):
    """反馈不存在或普通用户尝试访问不属于自己的反馈。"""


class FeedbackRepository(Protocol):
    """API 和知识候选导出器依赖的最小问题池接口。"""

    def record(
        self,
        *,
        turn: ConversationTurnRecord,
        owner_user_id: str,
        idempotency_key: str,
        signal: FeedbackSignal,
        reason: FeedbackReason | None,
    ) -> tuple[FeedbackRecord, bool]:
        """幂等保存一条用户或自动信号，返回记录和是否首次创建。"""

    def list_open(self, *, limit: int = 100) -> list[FeedbackRecord]:
        """按创建时间返回有限待处理问题。"""

    def review(
        self,
        *,
        feedback_id: UUID,
        reviewer_id: str,
        decision: FeedbackReview,
    ) -> FeedbackRecord:
        """幂等写入人工分类并在知识缺口时形成候选。"""

    def list_knowledge_candidates(self, *, limit: int = 100) -> list[KnowledgeCandidate]:
        """返回已经人工确认、尚待独立评测和发布的候选知识。"""

    def delete_for_conversation(self, *, conversation_id: UUID) -> int:
        """隐私删除时移除一段会话产生的全部反馈和候选正文。"""


def _new_record(
    *,
    turn: ConversationTurnRecord,
    owner_user_id: str,
    idempotency_key: str,
    signal: FeedbackSignal,
    reason: FeedbackReason | None,
) -> FeedbackRecord:
    """从可信轮次快照构造一条新反馈。"""

    now = datetime.now(UTC)
    return FeedbackRecord(
        feedback_id=uuid4(),
        idempotency_key=idempotency_key,
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        owner_user_id=owner_user_id,
        signal=signal,
        reason=reason,
        question=turn.standalone_question or turn.user_message,
        answer=turn.assistant_answer,
        intent=turn.intent,
        cited_document_ids=turn.cited_document_ids,
        created_at=now,
        updated_at=now,
    )


def _reviewed_record(
    record: FeedbackRecord,
    *,
    reviewer_id: str,
    decision: FeedbackReview,
) -> FeedbackRecord:
    """把开放反馈转换为稳定人工处置结果。"""

    status = (
        FeedbackStatus.KNOWLEDGE_CANDIDATE
        if decision.category == FeedbackCategory.KNOWLEDGE_GAP
        else (
            FeedbackStatus.DISMISSED
            if decision.category == FeedbackCategory.NOT_ACTIONABLE
            else FeedbackStatus.TRIAGED
        )
    )
    now = datetime.now(UTC)
    payload = record.model_dump(mode="python")
    payload.update(
        {
            "status": status,
            "category": decision.category,
            "reviewer_id": reviewer_id,
            "proposed_title": decision.proposed_title,
            "proposed_answer": decision.proposed_answer,
            "reviewed_at": now,
            "updated_at": now,
        }
    )
    return FeedbackRecord.model_validate(payload)


def _same_record_request(
    record: FeedbackRecord,
    *,
    turn: ConversationTurnRecord,
    owner_user_id: str,
    signal: FeedbackSignal,
    reason: FeedbackReason | None,
) -> bool:
    """判断幂等重试是否与第一次请求完全等价。"""

    return (
        record.turn_id == turn.turn_id
        and record.owner_user_id == owner_user_id
        and record.signal == signal
        and record.reason == reason
    )


def _same_review(
    record: FeedbackRecord,
    *,
    reviewer_id: str,
    decision: FeedbackReview,
) -> bool:
    """判断重复审核是否与已保存决定相同。"""

    return (
        record.reviewer_id == reviewer_id
        and record.category == decision.category
        and record.proposed_title == decision.proposed_title
        and record.proposed_answer == decision.proposed_answer
    )


def _candidate(record: FeedbackRecord) -> KnowledgeCandidate:
    """把已经通过领域校验的知识候选转换为发布输入。"""

    assert record.proposed_title is not None
    assert record.proposed_answer is not None
    assert record.reviewer_id is not None
    assert record.reviewed_at is not None
    return KnowledgeCandidate(
        candidate_id=record.feedback_id,
        source_feedback_id=record.feedback_id,
        title=record.proposed_title,
        content=record.proposed_answer,
        source_question=record.question,
        reviewer_id=record.reviewer_id,
        created_at=record.reviewed_at,
    )


class InMemoryFeedbackRepository:
    """测试和零持久化演示使用的线程安全反馈仓库。"""

    def __init__(self) -> None:
        self._records_by_id: dict[UUID, FeedbackRecord] = {}
        self._ids_by_idempotency_key: dict[tuple[str, str], UUID] = {}
        self._lock = Lock()

    def record(
        self,
        *,
        turn: ConversationTurnRecord,
        owner_user_id: str,
        idempotency_key: str,
        signal: FeedbackSignal,
        reason: FeedbackReason | None,
    ) -> tuple[FeedbackRecord, bool]:
        with self._lock:
            key = (owner_user_id, idempotency_key)
            existing_id = self._ids_by_idempotency_key.get(key)
            if existing_id is not None:
                existing = self._records_by_id[existing_id]
                if not _same_record_request(
                    existing,
                    turn=turn,
                    owner_user_id=owner_user_id,
                    signal=signal,
                    reason=reason,
                ):
                    raise FeedbackConflictError("反馈幂等键已用于另一请求")
                return existing, False
            record = _new_record(
                turn=turn,
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                signal=signal,
                reason=reason,
            )
            self._records_by_id[record.feedback_id] = record
            self._ids_by_idempotency_key[key] = record.feedback_id
            return record, True

    def list_open(self, *, limit: int = 100) -> list[FeedbackRecord]:
        records = [
            record
            for record in self._records_by_id.values()
            if record.status == FeedbackStatus.OPEN
        ]
        return sorted(records, key=lambda item: (item.created_at, str(item.feedback_id)))[:limit]

    def review(
        self,
        *,
        feedback_id: UUID,
        reviewer_id: str,
        decision: FeedbackReview,
    ) -> FeedbackRecord:
        with self._lock:
            existing = self._records_by_id.get(feedback_id)
            if existing is None:
                raise FeedbackNotFoundError
            if existing.status != FeedbackStatus.OPEN:
                if _same_review(existing, reviewer_id=reviewer_id, decision=decision):
                    return existing
                raise FeedbackConflictError("反馈已经由另一审核决定处置")
            reviewed = _reviewed_record(
                existing,
                reviewer_id=reviewer_id,
                decision=decision,
            )
            self._records_by_id[feedback_id] = reviewed
            return reviewed

    def list_knowledge_candidates(self, *, limit: int = 100) -> list[KnowledgeCandidate]:
        records = [
            record
            for record in self._records_by_id.values()
            if record.status == FeedbackStatus.KNOWLEDGE_CANDIDATE
        ]
        return [
            _candidate(record)
            for record in sorted(
                records,
                key=lambda item: (item.reviewed_at or item.updated_at, str(item.feedback_id)),
            )[:limit]
        ]

    def delete_for_conversation(self, *, conversation_id: UUID) -> int:
        with self._lock:
            ids = [
                feedback_id
                for feedback_id, record in self._records_by_id.items()
                if record.conversation_id == conversation_id
            ]
            for feedback_id in ids:
                record = self._records_by_id.pop(feedback_id)
                self._ids_by_idempotency_key.pop(
                    (record.owner_user_id, record.idempotency_key),
                    None,
                )
            return len(ids)


def _record_from_mapping(row: dict[str, Any]) -> FeedbackRecord:
    """把 SQLite/PostgreSQL 行恢复为同一领域对象。"""

    def decoded(name: str) -> Any:
        value = row[name]
        return json.loads(value) if isinstance(value, str) else value

    return FeedbackRecord.model_validate(
        {
            "feedback_id": row["feedback_id"],
            "idempotency_key": row["idempotency_key"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "owner_user_id": row["owner_user_id"],
            "signal": row["signal"],
            "reason": row["reason"],
            "status": row["status"],
            "category": row["category"],
            "question": row["question"],
            "answer": row["answer"],
            "intent": row["intent"],
            "cited_document_ids": decoded("cited_document_ids_json"),
            "reviewer_id": row["reviewer_id"],
            "proposed_title": row["proposed_title"],
            "proposed_answer": row["proposed_answer"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "reviewed_at": row["reviewed_at"],
        }
    )


class SQLiteFeedbackRepository:
    """单机学习环境使用的持久化反馈问题池。"""

    def __init__(self, *, database_path: Path) -> None:
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._setup_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _setup_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS feedback_items (
                    feedback_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    signal TEXT NOT NULL CHECK (signal IN ('helpful','unhelpful','auto_handoff')),
                    reason TEXT,
                    status TEXT NOT NULL CHECK (
                        status IN ('open','triaged','knowledge_candidate','dismissed')
                    ),
                    category TEXT,
                    question TEXT NOT NULL,
                    answer TEXT,
                    intent TEXT,
                    cited_document_ids_json TEXT NOT NULL,
                    reviewer_id TEXT,
                    proposed_title TEXT,
                    proposed_answer TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    UNIQUE (owner_user_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_status_created
                ON feedback_items (status, created_at);
                """
            )

    def _get_by_key(
        self,
        connection: sqlite3.Connection,
        *,
        owner_user_id: str,
        idempotency_key: str,
    ) -> FeedbackRecord | None:
        row = connection.execute(
            """
            SELECT * FROM feedback_items
            WHERE owner_user_id = ? AND idempotency_key = ?
            """,
            (owner_user_id, idempotency_key),
        ).fetchone()
        return _record_from_mapping(dict(row)) if row is not None else None

    def record(
        self,
        *,
        turn: ConversationTurnRecord,
        owner_user_id: str,
        idempotency_key: str,
        signal: FeedbackSignal,
        reason: FeedbackReason | None,
    ) -> tuple[FeedbackRecord, bool]:
        record = _new_record(
            turn=turn,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            signal=signal,
            reason=reason,
        )
        with self._connect() as connection:
            existing = self._get_by_key(
                connection,
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                if not _same_record_request(
                    existing,
                    turn=turn,
                    owner_user_id=owner_user_id,
                    signal=signal,
                    reason=reason,
                ):
                    raise FeedbackConflictError("反馈幂等键已用于另一请求")
                return existing, False
            connection.execute(
                """
                INSERT INTO feedback_items (
                    feedback_id,idempotency_key,conversation_id,turn_id,owner_user_id,
                    signal,reason,status,category,question,answer,intent,
                    cited_document_ids_json,reviewer_id,proposed_title,proposed_answer,
                    created_at,updated_at,reviewed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(record.feedback_id),
                    record.idempotency_key,
                    str(record.conversation_id),
                    str(record.turn_id),
                    record.owner_user_id,
                    record.signal.value,
                    record.reason.value if record.reason else None,
                    record.status.value,
                    None,
                    record.question,
                    record.answer,
                    record.intent,
                    json.dumps(record.cited_document_ids, ensure_ascii=False),
                    None,
                    None,
                    None,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    None,
                ),
            )
        return record, True

    def list_open(self, *, limit: int = 100) -> list[FeedbackRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM feedback_items WHERE status = 'open' ORDER BY created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [_record_from_mapping(dict(row)) for row in rows]

    def review(
        self,
        *,
        feedback_id: UUID,
        reviewer_id: str,
        decision: FeedbackReview,
    ) -> FeedbackRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM feedback_items WHERE feedback_id = ?",
                (str(feedback_id),),
            ).fetchone()
            if row is None:
                raise FeedbackNotFoundError
            existing = _record_from_mapping(dict(row))
            if existing.status != FeedbackStatus.OPEN:
                if _same_review(existing, reviewer_id=reviewer_id, decision=decision):
                    return existing
                raise FeedbackConflictError("反馈已经由另一审核决定处置")
            reviewed = _reviewed_record(
                existing,
                reviewer_id=reviewer_id,
                decision=decision,
            )
            connection.execute(
                """
                UPDATE feedback_items SET
                    status=?,category=?,reviewer_id=?,proposed_title=?,proposed_answer=?,
                    reviewed_at=?,updated_at=?
                WHERE feedback_id=? AND status='open'
                """,
                (
                    reviewed.status.value,
                    reviewed.category.value if reviewed.category else None,
                    reviewed.reviewer_id,
                    reviewed.proposed_title,
                    reviewed.proposed_answer,
                    reviewed.reviewed_at.isoformat() if reviewed.reviewed_at else None,
                    reviewed.updated_at.isoformat(),
                    str(feedback_id),
                ),
            )
            return reviewed

    def list_knowledge_candidates(self, *, limit: int = 100) -> list[KnowledgeCandidate]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM feedback_items
                WHERE status = 'knowledge_candidate'
                ORDER BY reviewed_at, feedback_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_candidate(_record_from_mapping(dict(row))) for row in rows]

    def delete_for_conversation(self, *, conversation_id: UUID) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM feedback_items WHERE conversation_id = ?",
                (str(conversation_id),),
            )
            return cursor.rowcount


class PostgresFeedbackRepository:
    """多实例运行时共享的 PostgreSQL 反馈问题池。"""

    def __init__(self, *, pool: PostgresConnectionPool) -> None:
        self._pool = pool

    def record(
        self,
        *,
        turn: ConversationTurnRecord,
        owner_user_id: str,
        idempotency_key: str,
        signal: FeedbackSignal,
        reason: FeedbackReason | None,
    ) -> tuple[FeedbackRecord, bool]:
        record = _new_record(
            turn=turn,
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            signal=signal,
            reason=reason,
        )
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT * FROM feedback_items
                WHERE owner_user_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (owner_user_id, idempotency_key),
            ).fetchone()
            if row is not None:
                existing = _record_from_mapping(dict(row))
                if not _same_record_request(
                    existing,
                    turn=turn,
                    owner_user_id=owner_user_id,
                    signal=signal,
                    reason=reason,
                ):
                    raise FeedbackConflictError("反馈幂等键已用于另一请求")
                return existing, False
            connection.execute(
                """
                INSERT INTO feedback_items (
                    feedback_id,idempotency_key,conversation_id,turn_id,owner_user_id,
                    signal,reason,status,category,question,answer,intent,
                    cited_document_ids_json,reviewer_id,proposed_title,proposed_answer,
                    created_at,updated_at,reviewed_at
                ) VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s
                )
                """,
                (
                    record.feedback_id,
                    record.idempotency_key,
                    record.conversation_id,
                    record.turn_id,
                    record.owner_user_id,
                    record.signal.value,
                    record.reason.value if record.reason else None,
                    record.status.value,
                    None,
                    record.question,
                    record.answer,
                    record.intent,
                    json.dumps(record.cited_document_ids, ensure_ascii=False),
                    None,
                    None,
                    None,
                    record.created_at,
                    record.updated_at,
                    None,
                ),
            )
        return record, True

    def list_open(self, *, limit: int = 100) -> list[FeedbackRecord]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM feedback_items
                WHERE status = 'open' ORDER BY created_at LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [_record_from_mapping(dict(row)) for row in rows]

    def review(
        self,
        *,
        feedback_id: UUID,
        reviewer_id: str,
        decision: FeedbackReview,
    ) -> FeedbackRecord:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                "SELECT * FROM feedback_items WHERE feedback_id = %s FOR UPDATE",
                (feedback_id,),
            ).fetchone()
            if row is None:
                raise FeedbackNotFoundError
            existing = _record_from_mapping(dict(row))
            if existing.status != FeedbackStatus.OPEN:
                if _same_review(existing, reviewer_id=reviewer_id, decision=decision):
                    return existing
                raise FeedbackConflictError("反馈已经由另一审核决定处置")
            reviewed = _reviewed_record(
                existing,
                reviewer_id=reviewer_id,
                decision=decision,
            )
            connection.execute(
                """
                UPDATE feedback_items SET
                    status=%s,category=%s,reviewer_id=%s,proposed_title=%s,
                    proposed_answer=%s,reviewed_at=%s,updated_at=%s
                WHERE feedback_id=%s AND status='open'
                """,
                (
                    reviewed.status.value,
                    reviewed.category.value if reviewed.category else None,
                    reviewed.reviewer_id,
                    reviewed.proposed_title,
                    reviewed.proposed_answer,
                    reviewed.reviewed_at,
                    reviewed.updated_at,
                    feedback_id,
                ),
            )
            return reviewed

    def list_knowledge_candidates(self, *, limit: int = 100) -> list[KnowledgeCandidate]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM feedback_items
                WHERE status = 'knowledge_candidate'
                ORDER BY reviewed_at, feedback_id LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return [_candidate(_record_from_mapping(dict(row))) for row in rows]

    def delete_for_conversation(self, *, conversation_id: UUID) -> int:
        with self._pool.connection() as connection, connection.transaction():
            cursor = connection.execute(
                "DELETE FROM feedback_items WHERE conversation_id = %s",
                (conversation_id,),
            )
            return cursor.rowcount
