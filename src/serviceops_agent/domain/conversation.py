"""多轮会话、单轮业务执行和可发送模型上下文的稳定领域契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ConversationStatus(StrEnum):
    """一段用户会话的有限生命周期。"""

    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class ConversationTurnStatus(StrEnum):
    """一轮用户消息对应的独立业务工作流状态。"""

    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class ExecutionKind(StrEnum):
    """一轮工作流可以被租约保护的两种执行入口。"""

    INITIAL = "initial"
    APPROVAL_RESUME = "approval_resume"


class ExecutionLeaseState(StrEnum):
    """执行租约的有限生命周期；终态租约不能继续发送心跳。"""

    ACTIVE = "active"
    RELEASED = "released"
    REVOKED = "revoked"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class ConversationMemory(BaseModel):
    """跨轮保存的有限结构化槽位，不把历史模型回答当成权威事实。"""

    model_config = ConfigDict(extra="forbid")

    memory_version: int = Field(default=0, ge=0)
    current_topic: str | None = Field(default=None, min_length=1, max_length=100)
    active_order_id: str | None = Field(default=None, pattern=r"^SO\d{6}$")
    recent_order_ids: list[str] = Field(default_factory=list, max_length=10)
    recent_document_ids: list[str] = Field(default_factory=list, max_length=10)
    last_intent: str | None = Field(default=None, min_length=1, max_length=100)
    # 防止较早但较慢的并发轮次覆盖较新轮次的主题和活动订单。
    last_processed_sequence: int = Field(default=0, ge=0)
    bounded_summary: str | None = Field(default=None, min_length=1, max_length=2000)
    # 0表示尚未生成摘要；正数是本次摘要窗口内最后一个已完成轮次。
    summary_window_end_sequence: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "summary_window_end_sequence",
            "summary_through_sequence",
        ),
    )

    @field_validator("active_order_id", mode="before")
    @classmethod
    def normalize_active_order_id(cls, value: object) -> object:
        """规范化用户历史中可能出现的小写订单号。"""

        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("recent_order_ids", mode="before")
    @classmethod
    def normalize_recent_order_ids(cls, value: object) -> object:
        """规范化并按首次出现顺序去重最近订单号。"""

        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            order_id = item.strip().upper()
            if order_id not in seen:
                normalized.append(order_id)
                seen.add(order_id)
        return normalized

    @field_validator("recent_order_ids")
    @classmethod
    def validate_recent_order_ids(cls, value: list[str]) -> list[str]:
        """保证结构化记忆不会保存任意字符串作为订单号。"""

        if any(
            len(order_id) != 8
            or not order_id.startswith("SO")
            or not order_id[2:].isdigit()
            for order_id in value
        ):
            raise ValueError("recent_order_ids 只能包含规范订单号")
        return value

    @field_validator("recent_document_ids", mode="before")
    @classmethod
    def normalize_recent_document_ids(cls, value: object) -> object:
        """按首次出现顺序去重知识文档ID。"""

        if not isinstance(value, list):
            return value
        normalized: list[object] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            document_id = item.strip()
            if document_id not in seen:
                normalized.append(document_id)
                seen.add(document_id)
        return normalized

    @field_validator("recent_document_ids")
    @classmethod
    def validate_recent_document_ids(cls, value: list[str]) -> list[str]:
        """知识来源槽位只保存有限、非空的稳定标识。"""

        if any(not document_id or len(document_id) > 100 for document_id in value):
            raise ValueError("recent_document_ids 只能包含1到100字符的文档ID")
        return value


class ConversationRecord(BaseModel):
    """会话仓库保存的所有权、生命周期和结构化记忆记录。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    owner_user_id: str = Field(min_length=1, max_length=200)
    status: ConversationStatus = ConversationStatus.ACTIVE
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        """拒绝无时区或时间顺序矛盾的持久化记录。"""

        timestamps = (self.created_at, self.updated_at, self.expires_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("会话时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at")
        return self


class ConversationDeletionPlan(BaseModel):
    """关闭会话后用于删除全部Checkpoint和业务轮次的内部计划。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    owner_user_id: str = Field(min_length=1, max_length=200)
    prepared_status: ConversationStatus
    workflow_thread_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_prepared_status(self) -> Self:
        """活动会话不能直接进入物理删除阶段。"""

        if self.prepared_status not in {
            ConversationStatus.CLOSED,
            ConversationStatus.EXPIRED,
        }:
            raise ValueError("删除计划必须来自已关闭或已过期会话")
        if len(self.workflow_thread_ids) != len(set(self.workflow_thread_ids)):
            raise ValueError("删除计划中的workflow_thread_ids不能重复")
        return self


class ConversationExecutionLease(BaseModel):
    """一轮工作流的排他执行权和单调 fencing generation。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    kind: ExecutionKind
    state: ExecutionLeaseState
    # Token只用于持有者证明身份；repr中隐藏，避免日志偶然泄漏。
    claim_token: UUID = Field(repr=False)
    fence_generation: int = Field(ge=1)
    # 审批恢复必须绑定已经落库的决定事件；初始执行不能伪造该来源。
    decision_audit_event_id: str | None = Field(
        default=None,
        min_length=36,
        max_length=36,
    )
    claimed_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime

    @model_validator(mode="after")
    def validate_execution_lease(self) -> Self:
        """校验租约来源、时区和活动期限，允许读取已经过期的陈旧记录。"""

        if self.kind == ExecutionKind.INITIAL:
            if self.decision_audit_event_id is not None:
                raise ValueError("初始执行租约不能包含审批决定事件")
        elif self.decision_audit_event_id is None:
            raise ValueError("审批恢复租约必须包含审批决定事件")

        timestamps = (self.claimed_at, self.heartbeat_at, self.lease_expires_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("执行租约时间必须包含时区")
        if self.heartbeat_at < self.claimed_at:
            raise ValueError("heartbeat_at 不能早于 claimed_at")
        if (
            self.state == ExecutionLeaseState.ACTIVE
            and self.lease_expires_at <= self.heartbeat_at
        ):
            raise ValueError("活动执行租约的到期时间必须晚于心跳时间")
        return self


class ConversationExecutionRecoveryResult(BaseModel):
    """一次陈旧执行恢复扫描的低敏分类计数，不包含用户或工作流内容。"""

    model_config = ConfigDict(extra="forbid")

    scanned_count: int = Field(ge=0)
    accepted_failed_count: int = Field(ge=0)
    initial_failed_count: int = Field(ge=0)
    approval_quarantined_count: int = Field(ge=0)
    legacy_manual_review_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_classified_count(self) -> Self:
        """同一条扫描记录最多归入一个处置分类。"""

        classified_count = (
            self.accepted_failed_count
            + self.initial_failed_count
            + self.approval_quarantined_count
            + self.legacy_manual_review_count
        )
        if classified_count > self.scanned_count:
            raise ValueError("执行恢复分类合计不能超过扫描数量")
        return self


class ConversationTurnRecord(BaseModel):
    """一次用户消息与一个独立 LangGraph 工作流之间的持久化映射。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    conversation_id: UUID
    workflow_thread_id: UUID
    sequence_number: int = Field(ge=1)
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    status: ConversationTurnStatus = ConversationTurnStatus.ACCEPTED
    user_message: str = Field(min_length=1, max_length=4000)
    standalone_question: str | None = Field(default=None, min_length=1, max_length=4000)
    assistant_answer: str | None = Field(default=None, min_length=1, max_length=4000)
    intent: str | None = Field(default=None, min_length=1, max_length=100)
    verified_order_ids: list[str] = Field(default_factory=list, max_length=10)
    cited_document_ids: list[str] = Field(default_factory=list, max_length=10)
    created_at: datetime
    updated_at: datetime

    @field_validator("verified_order_ids")
    @classmethod
    def validate_verified_order_ids(cls, value: list[str]) -> list[str]:
        """轮次记录只能保存已经通过工具边界的规范订单号。"""

        if any(
            len(order_id) != 8
            or not order_id.startswith("SO")
            or not order_id[2:].isdigit()
            for order_id in value
        ):
            raise ValueError("verified_order_ids 只能包含规范订单号")
        return value

    @field_validator("cited_document_ids")
    @classmethod
    def validate_cited_document_ids(cls, value: list[str]) -> list[str]:
        """轮次来源ID必须能安全进入有限结构化记忆。"""

        if any(not document_id or len(document_id) > 100 for document_id in value):
            raise ValueError("cited_document_ids 只能包含1到100字符的文档ID")
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        """保证仓库可以稳定按创建和更新时间排序。"""

        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("轮次时间必须包含时区")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        return self


class ConversationTurnUpdate(BaseModel):
    """仓库原子推进一轮工作流时允许修改的有限结果字段。"""

    model_config = ConfigDict(extra="forbid")

    expected_status: ConversationTurnStatus
    status: ConversationTurnStatus
    standalone_question: str | None = Field(default=None, min_length=1, max_length=4000)
    assistant_answer: str | None = Field(default=None, min_length=1, max_length=4000)
    intent: str | None = Field(default=None, min_length=1, max_length=100)
    verified_order_ids: list[str] = Field(default_factory=list, max_length=10)
    cited_document_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("verified_order_ids")
    @classmethod
    def validate_verified_order_ids(cls, value: list[str]) -> list[str]:
        """更新载荷不能绕过订单号格式边界。"""

        if any(
            len(order_id) != 8
            or not order_id.startswith("SO")
            or not order_id[2:].isdigit()
            for order_id in value
        ):
            raise ValueError("verified_order_ids 只能包含规范订单号")
        return value

    @field_validator("cited_document_ids")
    @classmethod
    def validate_cited_document_ids(cls, value: list[str]) -> list[str]:
        """更新载荷不能写入空或无界来源ID。"""

        if any(not document_id or len(document_id) > 100 for document_id in value):
            raise ValueError("cited_document_ids 只能包含1到100字符的文档ID")
        return value

    @model_validator(mode="after")
    def validate_transition(self) -> Self:
        """只接受单向有限状态转换，避免已完成轮次重新运行。"""

        allowed_transitions = {
            ConversationTurnStatus.ACCEPTED: {
                ConversationTurnStatus.RUNNING,
                ConversationTurnStatus.FAILED,
            },
            ConversationTurnStatus.RUNNING: {
                ConversationTurnStatus.WAITING_APPROVAL,
                ConversationTurnStatus.COMPLETED,
                ConversationTurnStatus.FAILED,
            },
            ConversationTurnStatus.WAITING_APPROVAL: {
                # RUNNING只为旧数据/兼容迁移保留；第47步审批API保持WAITING并用独立租约。
                ConversationTurnStatus.RUNNING,
                ConversationTurnStatus.COMPLETED,
                ConversationTurnStatus.FAILED,
            },
            ConversationTurnStatus.COMPLETED: set(),
            ConversationTurnStatus.FAILED: set(),
        }
        if self.status not in allowed_transitions[self.expected_status]:
            raise ValueError("不允许的会话轮次状态转换")
        if self.status == ConversationTurnStatus.COMPLETED and self.assistant_answer is None:
            raise ValueError("完成轮次必须包含 assistant_answer")
        return self


class RecentConversationTurn(BaseModel):
    """允许进入追问解析器的最小历史，不包含身份、审批或幂等字段。"""

    model_config = ConfigDict(extra="forbid")

    sequence_number: int = Field(ge=1)
    user_message: str = Field(min_length=1, max_length=4000)
    # standalone_question是当时经过安全解析的独立问题，可用于延续主题但不代表业务事实。
    standalone_question: str | None = Field(default=None, min_length=1, max_length=4000)
    intent: str | None = Field(default=None, min_length=1, max_length=100)
    verified_order_ids: list[str] = Field(default_factory=list, max_length=10)
    cited_document_ids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("verified_order_ids")
    @classmethod
    def validate_verified_order_ids(cls, value: list[str]) -> list[str]:
        """只允许已经由工具边界验证过的规范订单号进入模型上下文。"""

        if any(
            len(order_id) != 8
            or not order_id.startswith("SO")
            or not order_id[2:].isdigit()
            for order_id in value
        ):
            raise ValueError("verified_order_ids 只能包含规范订单号")
        return value

    @field_validator("cited_document_ids")
    @classmethod
    def validate_cited_document_ids(cls, value: list[str]) -> list[str]:
        """模型上下文中的来源ID保持与源轮次相同边界。"""

        if any(not document_id or len(document_id) > 100 for document_id in value):
            raise ValueError("cited_document_ids 只能包含1到100字符的文档ID")
        return value


class ConversationContext(BaseModel):
    """一轮执行开始前构造的有界会话快照。"""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
    recent_turns: list[RecentConversationTurn] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_turn_order(self) -> Self:
        """要求最近轮次严格递增，避免并发乱序产生含糊上下文。"""

        sequence_numbers = [turn.sequence_number for turn in self.recent_turns]
        if sequence_numbers != sorted(set(sequence_numbers)):
            raise ValueError("recent_turns 必须按不重复的 sequence_number 递增")
        return self


class FollowUpResolutionReason(StrEnum):
    """追问解析器允许公开和评测的有限决策原因。"""

    EXPLICIT_REFERENCE = "explicit_reference"
    VERIFIED_ORDER_REFERENCE = "verified_order_reference"
    AMBIGUOUS_ORDER_REFERENCE = "ambiguous_order_reference"
    PREVIOUS_TOPIC_REFERENCE = "previous_topic_reference"
    INDEPENDENT_QUESTION = "independent_question"


class FollowUpResolution(BaseModel):
    """把当前消息转换为可独立执行问题后的安全解析结果。"""

    model_config = ConfigDict(extra="forbid")

    standalone_question: str = Field(min_length=1, max_length=4000)
    reason: FollowUpResolutionReason
    used_context: bool
    needs_clarification: bool = False
    referenced_order_ids: list[str] = Field(default_factory=list, max_length=10)
    source_turn_sequence: int | None = Field(default=None, ge=1)
    source_memory: bool = False

    @field_validator("referenced_order_ids")
    @classmethod
    def validate_referenced_order_ids(cls, value: list[str]) -> list[str]:
        """解析器只能返回规范订单号。"""

        if any(
            len(order_id) != 8
            or not order_id.startswith("SO")
            or not order_id[2:].isdigit()
            for order_id in value
        ):
            raise ValueError("referenced_order_ids 只能包含规范订单号")
        return value

    @model_validator(mode="after")
    def validate_resolution_consistency(self) -> Self:
        """上下文标记、来源轮次和澄清原因必须保持一致。"""

        has_context_source = self.source_turn_sequence is not None or self.source_memory
        if self.used_context != has_context_source:
            raise ValueError("used_context 必须具有轮次或结构化记忆来源")
        if (
            self.reason == FollowUpResolutionReason.AMBIGUOUS_ORDER_REFERENCE
            and not self.needs_clarification
        ):
            raise ValueError("订单指代歧义必须要求澄清")
        return self
