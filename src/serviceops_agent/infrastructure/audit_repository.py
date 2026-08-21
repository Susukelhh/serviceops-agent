"""审批审计事件的内存/SQLite 追加仓库与哈希链校验。

仓库接口刻意不提供 update/delete。SQLite 还创建拒绝普通 UPDATE/DELETE 的触发器；
数据库管理员仍可删除触发器并重写整库，因此该方案是“可检测篡改”，不是绝对不可抵赖。
"""

# hmac.compare_digest 使用固定时间方式比较摘要，避免普通字符串比较的细微时序差异。
import hmac

# sqlite3 提供事务、唯一约束、WAL 与只追加保护触发器。
import sqlite3

# datetime/UTC 为新审计事件生成明确 UTC 时间。
from datetime import UTC, datetime

# Path 让数据库位置以绝对路径管理。
from pathlib import Path

# Lock 保护内存实现的“检查—计算前驱—追加”原子区间。
from threading import Lock

# Protocol 定义 API 依赖的最小仓库边界。
from typing import Protocol

# uuid5 基于线程与事件类型生成稳定事件编号，支持同一动作安全重放。
from uuid import NAMESPACE_URL, uuid5

# 领域模型和摘要函数保证两种存储实现使用完全相同的哈希规则。
from serviceops_agent.domain.audit import (
    AUDIT_GENESIS_HASH,
    ApprovalAuditDraft,
    ApprovalAuditEvent,
    calculate_event_hash,
)


class ApprovalAuditError(Exception):
    """审计仓库可预期错误的共同基类。"""


class ApprovalAuditConflictError(ApprovalAuditError):
    """同一线程事件类型已存在，但业务决定或可信主体不一致。"""


class ApprovalAuditRepository(Protocol):
    """审批 API 和只读审计接口依赖的最小仓库协议。"""

    def append(self, draft: ApprovalAuditDraft) -> tuple[ApprovalAuditEvent, bool]:
        """追加新事件或返回完全相同的既有事件；布尔值表示幂等重放。"""

    def list_for_thread(self, thread_id: str) -> list[ApprovalAuditEvent]:
        """按链位置返回某一线程的全部事件。"""

    def verify_thread_chain(self, thread_id: str) -> bool:
        """重新计算并验证某一线程的前驱引用、位置和事件哈希。"""


def _stable_event_id(draft: ApprovalAuditDraft) -> str:
    """根据线程和有限事件类型生成确定性 UUID。"""

    # 同一线程的同一语义事件只能有一个稳定标识。
    stable_uuid = uuid5(
        NAMESPACE_URL,
        f"serviceops-approval-audit:{draft.thread_id}:{draft.event_type.value}",
    )
    # str 产生 API 友好的标准 36 字符 UUID 表示。
    return str(stable_uuid)


def _draft_matches_event(
    draft: ApprovalAuditDraft,
    event: ApprovalAuditEvent,
) -> bool:
    """判断重复追加是否代表相同业务动作。"""

    # token_jti 是一次认证凭证元数据；Token 续期后的相同主体重试仍应安全返回原事件。
    draft_payload = draft.model_dump(mode="json", exclude={"token_jti"})
    # 已存事件还包含仓库生成字段，因此只选取草稿字段并同样忽略 token_jti。
    event_payload = event.model_dump(
        mode="json",
        include=set(ApprovalAuditDraft.model_fields) - {"token_jti"},
    )
    # 完整比较主体、决定、草案摘要、备注摘要和结果编号，避免冲突被当作重放。
    return draft_payload == event_payload


def _build_event(
    *,
    draft: ApprovalAuditDraft,
    chain_position: int,
    previous_event_hash: str,
    created_at: datetime | None = None,
) -> ApprovalAuditEvent:
    """创建事件并计算覆盖全部字段的最终哈希。"""

    # 先使用占位哈希构造完整强类型对象，让所有其他字段先通过领域校验。
    unhashed_event = ApprovalAuditEvent(
        # 展开草稿中的受控业务字段。
        **draft.model_dump(),
        # 稳定事件 ID 支持重试定位同一语义事件。
        audit_event_id=_stable_event_id(draft),
        # 当前线程内严格递增位置。
        chain_position=chain_position,
        # 默认使用当前 UTC 时间；数据库读取校验不会调用本构造器。
        created_at=created_at or datetime.now(UTC),
        # 前驱哈希把事件串成单向链。
        previous_event_hash=previous_event_hash,
        # 先放合法格式占位值，下一行会根据完整 Payload 替换。
        event_hash=AUDIT_GENESIS_HASH,
    )
    # model_copy 创建最终不可变语义副本，不原地篡改已校验模型。
    return unhashed_event.model_copy(
        update={"event_hash": calculate_event_hash(unhashed_event)}
    )


def _verify_events(events: list[ApprovalAuditEvent]) -> bool:
    """验证已经按链位置排序的一组事件。"""

    # 第一条必须引用公开 Genesis Hash。
    expected_previous_hash = AUDIT_GENESIS_HASH
    # enumerate 从 1 开始，使缺失或重复位置都能被检测。
    for expected_position, event in enumerate(events, start=1):
        # 链位置必须连续，不能静默跳过中间事件。
        if event.chain_position != expected_position:
            return False
        # 当前事件必须准确引用上一条已验证事件。
        if not hmac.compare_digest(
            event.previous_event_hash,
            expected_previous_hash,
        ):
            return False
        # 对磁盘/内存字段重新计算哈希，而不是相信保存的 event_hash。
        recalculated_hash = calculate_event_hash(event)
        # 内容、时间、主体或前驱任一字段被修改都会在这里失败。
        if not hmac.compare_digest(event.event_hash, recalculated_hash):
            return False
        # 下一条应引用当前已经验证的哈希。
        expected_previous_hash = event.event_hash
    # 空列表表示“尚无事件”而不是“存在一条坏链”；查询接口会另外返回 404。
    return True


class InMemoryApprovalAuditRepository:
    """用于自动测试和单进程演示的线程安全只追加审计仓库。"""

    def __init__(self) -> None:
        """初始化按线程分组的事件列表和互斥锁。"""

        # 每个线程维护自己的链，避免无关审批事件相互阻塞查询语义。
        self._events_by_thread: dict[str, list[ApprovalAuditEvent]] = {}
        # Lock 保证并发追加时不会得到相同位置或错误前驱。
        self._lock = Lock()

    def append(self, draft: ApprovalAuditDraft) -> tuple[ApprovalAuditEvent, bool]:
        """在锁内完成重复检查、哈希计算和最多一次追加。"""

        # 整个追加过程是一个短原子区间。
        with self._lock:
            # setdefault 首次看到线程时创建空链。
            thread_events = self._events_by_thread.setdefault(draft.thread_id, [])
            # 同一线程同一事件类型只允许出现一次。
            existing = next(
                (
                    event
                    for event in thread_events
                    if event.event_type == draft.event_type
                ),
                None,
            )
            # 找到既有事件时区分安全重放与语义冲突。
            if existing is not None:
                if _draft_matches_event(draft, existing):
                    return existing, True
                raise ApprovalAuditConflictError
            # 空链从 Genesis 开始，否则引用最后一条事件哈希。
            previous_hash = (
                thread_events[-1].event_hash if thread_events else AUDIT_GENESIS_HASH
            )
            # 新位置等于当前长度加一。
            event = _build_event(
                draft=draft,
                chain_position=len(thread_events) + 1,
                previous_event_hash=previous_hash,
            )
            # 只有全部校验和哈希计算成功后才追加。
            thread_events.append(event)
            # False 表示本次实际新增一条事件。
            return event, False

    def list_for_thread(self, thread_id: str) -> list[ApprovalAuditEvent]:
        """返回事件列表副本，防止调用方直接修改仓库内部容器。"""

        # 读取同样加锁，避免与并发追加交错。
        with self._lock:
            # list(...) 创建浅副本；Pydantic 事件按约定不由调用方变更。
            return list(self._events_by_thread.get(thread_id, []))

    def verify_thread_chain(self, thread_id: str) -> bool:
        """对当前线程事件副本执行完整哈希链校验。"""

        # list_for_thread 已在锁内取得一致快照。
        return _verify_events(self.list_for_thread(thread_id))


class SQLiteApprovalAuditRepository:
    """使用 SQLite 事务、唯一约束和触发器的跨重启审计仓库。"""

    def __init__(self, *, database_path: Path) -> None:
        """保存绝对路径、创建父目录并幂等初始化表和触发器。"""

        # resolve 消除 PyCharm Working directory 对数据库位置的影响。
        self._database_path = database_path.resolve()
        # SQLite 不会自动创建父目录。
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        # 启动即建表；失败会阻止应用假装健康运行。
        self._setup_schema()

    def _connect(self) -> sqlite3.Connection:
        """创建一次短连接并启用锁等待与外键选项。"""

        # 每个操作独立连接，避免跨线程共享 sqlite3.Connection。
        connection = sqlite3.connect(
            str(self._database_path),
            timeout=5.0,
            isolation_level=None,
        )
        # Row 允许按列名恢复领域模型。
        connection.row_factory = sqlite3.Row
        # 为后续关联表预先启用外键。
        connection.execute("PRAGMA foreign_keys = ON")
        # 并发写锁最多等待五秒。
        connection.execute("PRAGMA busy_timeout = 5000")
        # 返回由调用方法最终关闭的连接。
        return connection

    def _setup_schema(self) -> None:
        """创建审计表、链约束以及阻止普通修改/删除的触发器。"""

        # 启动迁移使用短连接。
        connection = self._connect()
        try:
            # WAL 改善本地一个写者和多个读者的并发体验。
            connection.execute("PRAGMA journal_mode = WAL")
            # 表内同时约束稳定事件 ID、线程事件类型和线程链位置唯一。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_audit_events (
                    audit_event_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    token_jti TEXT NOT NULL,
                    approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                    order_id TEXT NOT NULL,
                    proposal_digest TEXT NOT NULL,
                    comment_digest TEXT NOT NULL,
                    return_request_id TEXT,
                    chain_position INTEGER NOT NULL CHECK (chain_position >= 1),
                    created_at TEXT NOT NULL,
                    previous_event_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE,
                    UNIQUE (thread_id, event_type),
                    UNIQUE (thread_id, chain_position)
                )
                """
            )
            # 该索引加速审计员按线程顺序读取完整证据链。
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_approval_audit_thread_position
                ON approval_audit_events (thread_id, chain_position)
                """
            )
            # 应用普通 SQL UPDATE 会被拒绝，迫使修正以新事件表达而不是覆盖历史。
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_approval_audit_update
                BEFORE UPDATE ON approval_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'approval audit events are append-only');
                END
                """
            )
            # 应用普通 SQL DELETE 同样被拒绝。
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_approval_audit_delete
                BEFORE DELETE ON approval_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'approval audit events are append-only');
                END
                """
            )
        finally:
            # 无论迁移成功与否都关闭文件句柄。
            connection.close()

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ApprovalAuditEvent:
        """把 SQLite 行重新交给 Pydantic 领域模型完整校验。"""

        # 不信任磁盘字段格式；任何非法枚举、摘要或时间都会抛出校验异常。
        return ApprovalAuditEvent.model_validate(dict(row))

    def append(self, draft: ApprovalAuditDraft) -> tuple[ApprovalAuditEvent, bool]:
        """在 BEGIN IMMEDIATE 事务中原子检查并追加一条链事件。"""

        # 每次追加使用独立连接。
        connection = self._connect()
        try:
            # 在读取尾事件前取得保留写锁，避免并发请求使用相同前驱和位置。
            connection.execute("BEGIN IMMEDIATE")
            # 先按语义唯一键查找幂等重放。
            existing_row = connection.execute(
                """
                SELECT audit_event_id, thread_id, event_type, request_id, actor_id,
                       token_jti, approved, order_id, proposal_digest, comment_digest,
                       return_request_id, chain_position, created_at,
                       previous_event_hash, event_hash
                FROM approval_audit_events
                WHERE thread_id = ? AND event_type = ?
                """,
                (draft.thread_id, draft.event_type.value),
            ).fetchone()
            # 已存在时只能返回相同业务动作。
            if existing_row is not None:
                existing = self._event_from_row(existing_row)
                if _draft_matches_event(draft, existing):
                    connection.commit()
                    return existing, True
                raise ApprovalAuditConflictError

            # 查询当前线程最后一条事件，得到新位置与前驱摘要。
            last_row = connection.execute(
                """
                SELECT chain_position, event_hash
                FROM approval_audit_events
                WHERE thread_id = ?
                ORDER BY chain_position DESC
                LIMIT 1
                """,
                (draft.thread_id,),
            ).fetchone()
            # 新线程从位置 1 和 Genesis Hash 开始。
            chain_position = 1 if last_row is None else int(last_row["chain_position"]) + 1
            previous_hash = (
                AUDIT_GENESIS_HASH if last_row is None else str(last_row["event_hash"])
            )
            # 在持锁事务内生成时间和最终摘要。
            event = _build_event(
                draft=draft,
                chain_position=chain_position,
                previous_event_hash=previous_hash,
            )
            # 参数化 INSERT 防止外部标识进入 SQL 语法。
            connection.execute(
                """
                INSERT INTO approval_audit_events (
                    audit_event_id, thread_id, event_type, request_id, actor_id,
                    token_jti, approved, order_id, proposal_digest, comment_digest,
                    return_request_id, chain_position, created_at,
                    previous_event_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.audit_event_id,
                    event.thread_id,
                    event.event_type.value,
                    event.request_id,
                    event.actor_id,
                    event.token_jti,
                    int(event.approved),
                    event.order_id,
                    event.proposal_digest,
                    event.comment_digest,
                    event.return_request_id,
                    event.chain_position,
                    event.created_at.isoformat(),
                    event.previous_event_hash,
                    event.event_hash,
                ),
            )
            # 只有插入成功后提交，让其他实例看到完整事件。
            connection.commit()
            return event, False
        except Exception:
            # 冲突、校验或 SQLite 异常都回滚未完成事务。
            connection.rollback()
            raise
        finally:
            # 最终释放数据库连接和锁。
            connection.close()

    def list_for_thread(self, thread_id: str) -> list[ApprovalAuditEvent]:
        """按链位置读取某一线程全部事件并执行领域校验。"""

        # 查询使用独立短连接。
        connection = self._connect()
        try:
            # ORDER BY 是哈希链验证的必要前提。
            rows = connection.execute(
                """
                SELECT audit_event_id, thread_id, event_type, request_id, actor_id,
                       token_jti, approved, order_id, proposal_digest, comment_digest,
                       return_request_id, chain_position, created_at,
                       previous_event_hash, event_hash
                FROM approval_audit_events
                WHERE thread_id = ?
                ORDER BY chain_position ASC
                """,
                (thread_id,),
            ).fetchall()
            # 每一行都必须重新通过 Pydantic 校验后才能返回。
            return [self._event_from_row(row) for row in rows]
        finally:
            # 只读查询同样及时关闭句柄。
            connection.close()

    def verify_thread_chain(self, thread_id: str) -> bool:
        """从磁盘重新读取并验证完整链。"""

        # list_for_thread 返回排序且经过 Schema 校验的事件。
        return _verify_events(self.list_for_thread(thread_id))
