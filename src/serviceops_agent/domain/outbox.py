"""事务 Outbox 使用的稳定领域模型和确定性事件编号。

Outbox 的职责不是再次保存完整退货原因或审批备注，而是在业务事务内保存一份
“退货申请已经提交”的最小事件。后台协调器随后把它投递到审批审计哈希链。
"""

# datetime 表达事件创建、下次重试和完成时间；StrEnum 限制事件与处理状态。
from datetime import datetime
from enum import StrEnum

# Literal 把已批准事实固定为 True，防止拒绝决定误生成业务提交事件。
from typing import Literal

# UUID5 根据工作流线程生成稳定事件 ID，支持投递过程安全重放。
from uuid import NAMESPACE_URL, uuid5

# BaseModel 提供跨存储实现的一致校验；ConfigDict 禁止额外字段并冻结可信元数据。
from pydantic import BaseModel, ConfigDict, Field


class OutboxEventType(StrEnum):
    """当前 Outbox 允许保存的有限事件类型。"""

    # RETURN_REQUEST_COMMITTED 表示退货申请记录已经与本事件在同一事务提交。
    RETURN_REQUEST_COMMITTED = "return_request_committed"


class OutboxStatus(StrEnum):
    """一条 Outbox 事件的有限处理状态。"""

    # PENDING 表示等待首次投递或到达下次重试时间。
    PENDING = "pending"
    # PROCESSED 表示下游审计事件已经新增或被幂等确认存在。
    PROCESSED = "processed"
    # DEAD_LETTER 表示连续失败达到上限，必须由运维人员检查后再处置。
    DEAD_LETTER = "dead_letter"


class ReturnOutboxMetadata(BaseModel):
    """API 在批准恢复时注入、但不会暴露给模型 Tool Schema 的可信元数据。"""

    # 冻结对象避免仓库事务执行期间被调用方原地修改；额外字段直接拒绝。
    model_config = ConfigDict(frozen=True, extra="forbid")

    # thread_id 来自 FastAPI 路径，并指向本次可恢复 LangGraph 线程。
    thread_id: str = Field(min_length=1, max_length=100)
    # request_id 来自首次创建的 Checkpoint State，用于关联原始请求。
    request_id: str = Field(min_length=1, max_length=100)
    # actor_id 来自已验签 JWT 的 sub，不接受审批请求体覆盖。
    actor_id: str = Field(min_length=1, max_length=64)
    # token_jti 来自已验签 JWT 的 jti；Outbox 不保存完整 Bearer Token。
    token_jti: str = Field(min_length=8, max_length=100)
    # approved 只能为字面量 True；拒绝流程不会进入退货业务写事务。
    approved: Literal[True]
    # order_id 来自已经人工审阅并重新校验的退货草案。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # proposal_digest 是草案的 SHA-256，不复制原因和幂等键原文。
    proposal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    # comment_digest 是审批备注的 SHA-256，不复制自由文本备注。
    comment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReturnCommittedEventPayload(ReturnOutboxMetadata):
    """业务记录创建后可以完整投递为审计完成事件的最小载荷。"""

    # return_request_id 只能由仓库在构造业务记录后补入，调用者不能提前猜测结果。
    return_request_id: str = Field(pattern=r"^RR-[A-F0-9]{12}$")


class OutboxEventRecord(BaseModel):
    """Outbox 仓库已经保存的一条不可变业务事件及可变投递状态快照。"""

    # event_id 由 thread_id 和固定事件类型确定，重复恢复会定位同一事件。
    event_id: str = Field(min_length=36, max_length=36)
    # event_type 当前只允许退货申请已提交事件。
    event_type: OutboxEventType
    # aggregate_type 明确本事件关联的是退货申请聚合。
    aggregate_type: str = Field(pattern=r"^return_request$")
    # aggregate_id 是实际生成的退货申请编号。
    aggregate_id: str = Field(pattern=r"^RR-[A-F0-9]{12}$")
    # payload 是经过强类型校验的最小下游审计载荷。
    payload: ReturnCommittedEventPayload
    # status 区分待处理、已处理和死信。
    status: OutboxStatus
    # attempts 只统计失败投递次数；成功首次投递仍为零。
    attempts: int = Field(ge=0)
    # next_attempt_at 控制退避重试；死信不会再被普通扫描器选中。
    next_attempt_at: datetime
    # created_at 是业务记录与 Outbox 事件共同提交的 UTC 时间。
    created_at: datetime
    # processed_at 只在成功或幂等投递后存在。
    processed_at: datetime | None = None
    # last_error_code 只保存有限内部错误码，不保存异常正文或敏感载荷。
    last_error_code: str | None = Field(default=None, max_length=100)


class ReconciliationBatchResult(BaseModel):
    """一次 Outbox 协调批次对外公开的低敏统计结果。"""

    # scanned 是本批实际取得的到期 pending 事件数。
    scanned: int = Field(ge=0)
    # processed 是本批首次向审计仓库新增并确认完成的数量。
    processed: int = Field(ge=0)
    # replayed 是审计事件已存在、经幂等比较后确认完成的数量。
    replayed: int = Field(ge=0)
    # failed 是本批投递失败且仍可在退避后重试的数量。
    failed: int = Field(ge=0)
    # dead_letter 是本批达到最大尝试次数并转为死信的数量。
    dead_letter: int = Field(ge=0)


def build_return_outbox_event_id(thread_id: str) -> str:
    """根据线程和有限事件类型生成跨进程稳定的 Outbox UUID。"""

    # 名称中包含固定语义前缀，避免与项目中其他 UUID5 用途发生碰撞。
    stable_uuid = uuid5(
        NAMESPACE_URL,
        f"serviceops-outbox:{thread_id}:{OutboxEventType.RETURN_REQUEST_COMMITTED.value}",
    )
    # 标准 UUID 字符串固定为 36 字符，便于 SQLite 主键和接口日志使用。
    return str(stable_uuid)
