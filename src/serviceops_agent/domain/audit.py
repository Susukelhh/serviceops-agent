"""审批审计事件、内容摘要和哈希链使用的领域模型。

该模块只定义稳定数据契约和确定性摘要算法，不负责选择 SQLite 或内存存储。
审计库只保存业务内容的 SHA-256 摘要，不复制退货原因、幂等键、审批备注或完整 JWT。
"""

# hashlib 提供 SHA-256；json 生成字段顺序稳定的规范化字节。
import hashlib
import json

# datetime 表达审计事件的 UTC 时间；StrEnum 限制事件类型。
from datetime import datetime
from enum import StrEnum

# Any 标注可以被规范 JSON 序列化的受控领域数据。
from typing import Any

# BaseModel 提供强类型校验；ConfigDict 冻结已落库事件；Field 约束标识、摘要和链位置。
from pydantic import BaseModel, ConfigDict, Field

# ReturnRequestProposal 是人工批准前由系统生成并保存在 Checkpoint 中的可信草案。
from serviceops_agent.domain.returns import ReturnRequestProposal

# 第一条事件没有前驱，因此使用固定的 64 位零摘要作为公开链起点。
AUDIT_GENESIS_HASH = "0" * 64


class ApprovalAuditEventType(StrEnum):
    """退货审批证据链允许出现的有限事件类型。"""

    # DECISION_RECORDED 表示合法审批主体已提交批准或拒绝决定。
    DECISION_RECORDED = "approval_decision_recorded"
    # WORKFLOW_COMPLETED 表示批准后的写工具已成功或安全幂等重放。
    WORKFLOW_COMPLETED = "workflow_completed"
    # WORKFLOW_REJECTED 表示人工明确拒绝，工作流以零业务写入结束。
    WORKFLOW_REJECTED = "workflow_rejected"
    # WORKFLOW_FAILED 表示决定已记录，但恢复或写工具没有得到成功终态。
    WORKFLOW_FAILED = "workflow_failed"


class ApprovalAuditDraft(BaseModel):
    """API 请求仓库追加的一条审计事件草稿。"""

    # thread_id 关联 LangGraph Checkpoint，但本身不包含状态快照正文。
    thread_id: str = Field(min_length=1, max_length=100)
    # event_type 只能从上面的有限枚举中选择，避免自由事件名污染审计语义。
    event_type: ApprovalAuditEventType
    # request_id 关联最初的 Agent 请求。
    request_id: str = Field(min_length=1, max_length=100)
    # actor_id 来自已验证 JWT 的 sub，而不是审批请求体。
    actor_id: str = Field(min_length=1, max_length=64)
    # token_jti 来自已验证 JWT 的 jti；不会保存完整 Bearer Token。
    token_jti: str = Field(min_length=8, max_length=100)
    # approved 保存决定的严格布尔语义，结果事件继续携带它以自描述。
    approved: bool
    # order_id 是草案中的规范订单号。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # proposal_digest 是完整可信草案的 SHA-256，不复制原因或幂等键。
    proposal_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    # comment_digest 是规范化审批备注的 SHA-256，不保存可能敏感的备注原文。
    comment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    # return_request_id 只在批准且写工具成功后的结果事件中出现。
    return_request_id: str | None = Field(
        default=None,
        pattern=r"^RR-[A-F0-9]{12}$",
    )


class ApprovalAuditEvent(ApprovalAuditDraft):
    """仓库已经追加并绑定到前序哈希的一条不可变审计事件。"""

    # 已落库事件禁止调用方原地赋值；修正历史必须追加新语义事件而不是覆盖字段。
    model_config = ConfigDict(frozen=True)

    # audit_event_id 根据 thread_id 与事件类型稳定生成，支持请求安全重放。
    audit_event_id: str = Field(min_length=36, max_length=36)
    # chain_position 是当前线程内从 1 开始的严格递增位置。
    chain_position: int = Field(ge=1)
    # created_at 使用带时区 UTC 时间，避免服务器本地时区歧义。
    created_at: datetime
    # previous_event_hash 指向同线程上一条事件；第一条指向公开 Genesis Hash。
    previous_event_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    # event_hash 覆盖本事件所有公开字段以及 previous_event_hash。
    event_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def _sha256_text(value: str) -> str:
    """把 UTF-8 文本转换成固定 64 位小写 SHA-256 十六进制摘要。"""

    # encode 明确使用 UTF-8，保证 Windows 与 Linux 对中文输入得到相同摘要。
    encoded_value = value.encode("utf-8")
    # hexdigest 返回便于 SQLite、JSON 和人工比对的固定长度字符串。
    return hashlib.sha256(encoded_value).hexdigest()


def canonical_json_digest(value: Any) -> str:
    """对受控 JSON 数据生成跨进程稳定的 SHA-256 摘要。"""

    # sort_keys 固定字段顺序；紧凑 separators 排除无意义空白差异。
    canonical_json = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # 最终只返回不可逆摘要，不把原始敏感业务字段复制到审计表。
    return _sha256_text(canonical_json)


def build_proposal_digest(proposal: ReturnRequestProposal) -> str:
    """对经过 Pydantic 校验的退货草案生成稳定摘要。"""

    # mode=json 把所有字段转换为规范 JSON 类型后再计算摘要。
    return canonical_json_digest(proposal.model_dump(mode="json"))


def build_comment_digest(comment: str) -> str:
    """规范化审批备注空白并生成摘要，避免记录自由文本原文。"""

    # split/join 会去除首尾空白，并把换行、制表符和连续空格归一为单空格。
    normalized_comment = " ".join(comment.split())
    # 即使备注为空也得到稳定摘要，避免用 None 混淆“空备注”和“字段缺失”。
    return _sha256_text(normalized_comment)


def build_event_hash_payload(event: ApprovalAuditEvent) -> dict[str, object]:
    """提取哈希覆盖字段；event_hash 自身不能参与递归计算。"""

    # Pydantic 的 json 模式会把枚举和 datetime 转成稳定字符串。
    payload = event.model_dump(mode="json")
    # event_hash 是本函数的计算结果，因此必须从输入副本中删除。
    del payload["event_hash"]
    # dict 的键顺序不影响后续 canonical_json_digest，因为它会再次排序。
    return payload


def calculate_event_hash(event: ApprovalAuditEvent) -> str:
    """重新计算一条完整审计事件应有的哈希。"""

    # 哈希覆盖事件内容、链位置、创建时间和前驱哈希，任一字段变化都会改变结果。
    return canonical_json_digest(build_event_hash_payload(event))
