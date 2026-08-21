"""退货申请、人工审批和幂等写工具使用的稳定领域模型。"""

# datetime 表达写入记录创建时间；StrEnum 限制流程和业务状态。
from datetime import datetime
from enum import StrEnum

# BaseModel 提供运行时校验；StrictBool 禁止字符串真值；model_validator 校验结果一致性。
from pydantic import BaseModel, Field, StrictBool, model_validator


class ReturnRequestStatus(StrEnum):
    """退货申请写入业务仓库后的有限状态。"""

    # SUBMITTED 表示申请已创建，等待后续售后审核或处理。
    SUBMITTED = "submitted"


class ReturnWorkflowStatus(StrEnum):
    """LangGraph 退货审批工作流的有限阶段。"""

    # CLARIFICATION 表示缺少订单号、原因或订单不可用，需要用户修正。
    CLARIFICATION = "clarification"
    # DECLINED 表示确定性业务前置条件不允许发起申请。
    DECLINED = "declined"
    # APPROVAL_PENDING 表示草案已准备，图将在 interrupt 节点暂停。
    APPROVAL_PENDING = "approval_pending"
    # APPROVED 表示人工明确批准，可以进入写工具节点。
    APPROVED = "approved"
    # REJECTED 表示人工拒绝，本轮禁止执行写工具。
    REJECTED = "rejected"
    # COMPLETED 表示批准后的写工具已经成功或幂等返回现有记录。
    COMPLETED = "completed"
    # FAILED 表示恢复值、工具或写入结果异常，需要人工排查。
    FAILED = "failed"


class ReturnRequestProposal(BaseModel):
    """执行写操作前供人工审阅的退货申请草案。"""

    # action 固定为 create_return_request，审批人不会面对任意模型动作文本。
    action: str = Field(pattern=r"^create_return_request$")
    # order_id 是已经完成归属与状态预检查的订单号。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # reason 是用户明确提供的退货原因，不由模型自行补写。
    reason: str = Field(min_length=5, max_length=500)
    # idempotency_key 由 API 请求或系统生成，用于重复恢复/重试去重。
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    # risk_level 固定标记为 write，前端可据此使用高风险审批样式。
    risk_level: str = Field(pattern=r"^write$")


class ApprovalRequestPayload(BaseModel):
    """通过 LangGraph interrupt 暴露给审批客户端的最小安全负载。"""

    # kind 允许 API 确认当前中断确实属于退货写操作审批。
    kind: str = Field(pattern=r"^return_request_approval$")
    # request_id 关联原始请求日志，但不包含用户自然语言全文。
    request_id: str = Field(min_length=1, max_length=100)
    # action 是等待批准的稳定业务动作。
    action: str = Field(pattern=r"^create_return_request$")
    # order_id 是目标订单号。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # reason 是审批人必须看到的用户申请原因。
    reason: str = Field(min_length=5, max_length=500)
    # risk_level 明确这是会产生业务记录的写操作。
    risk_level: str = Field(pattern=r"^write$")
    # message 使用固定本地文案说明批准的实际效果。
    message: str = Field(min_length=1, max_length=300)


class ApprovalDecision(BaseModel):
    """审批接口恢复 interrupt 时必须提交的结构化决定。"""

    # approved=True 才允许后续条件边进入写工具节点。
    approved: StrictBool
    # reviewer_id 在生产 API 中只从 RBAC/JWT 主体注入；直接图示例使用显式演示值。
    reviewer_id: str = Field(min_length=1, max_length=64)
    # comment 保存简短审批备注，不接收无边界自由文本。
    comment: str = Field(default="", max_length=500)
    # thread_id 由生产 API 路径注入，用于生成稳定 Outbox 事件；模型和请求体不可填写。
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    # token_jti 由验签后的 JWT 注入；只保存唯一编号，不保存完整 Bearer Token。
    token_jti: str | None = Field(default=None, min_length=8, max_length=100)

    @model_validator(mode="after")
    def validate_trusted_outbox_context(self) -> "ApprovalDecision":
        """保证生产 Outbox 所需的 thread_id 与 token_jti 必须同时存在或同时缺失。"""

        # 一个字段缺失会让审计事件无法完整关联，因此不能静默降级为半可信元数据。
        if (self.thread_id is None) != (self.token_jti is None):
            raise ValueError("thread_id 与 token_jti 必须同时存在或同时缺失")
        # 两者都缺失只允许直接 LangGraph 教学调用，不影响原有离线示例。
        return self


class ReturnRequestRecord(BaseModel):
    """退货申请仓库中已经成功写入的一条业务记录。"""

    # return_request_id 是可以返回用户和客服系统的稳定申请编号。
    return_request_id: str = Field(pattern=r"^RR-[A-F0-9]{12}$")
    # order_id 是申请对应的本人已签收订单。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # user_id 保留真实归属，但不会暴露给模型规划参数。
    user_id: str = Field(min_length=1, max_length=64)
    # reason 保存经过边界校验的用户原因。
    reason: str = Field(min_length=5, max_length=500)
    # status 是写入后的有限业务状态。
    status: ReturnRequestStatus
    # idempotency_key 关联重复 HTTP/恢复请求。
    idempotency_key: str = Field(min_length=8, max_length=128)
    # created_at 使用带时区时间，避免服务器本地时区歧义。
    created_at: datetime


class ReturnRequestResult(BaseModel):
    """写工具返回给 LangGraph 执行节点的结构化结果。"""

    # success 表示申请已经存在或本次成功创建。
    success: bool
    # created 区分本次新写入和幂等返回既有记录。
    created: bool
    # idempotent_replay=True 表示相同键与相同负载已经处理过。
    idempotent_replay: bool
    # order_id 始终回显经过 Tool Schema 校验的目标订单。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # return_request_id 只在成功时存在。
    return_request_id: str | None = Field(default=None, pattern=r"^RR-[A-F0-9]{12}$")
    # status 只在成功时存在。
    status: ReturnRequestStatus | None = None
    # outbox_event_id 只在生产 API 注入可信审批上下文时存在，不属于模型工具参数。
    outbox_event_id: str | None = Field(default=None, min_length=36, max_length=36)
    # failure_code 只在业务拒绝或幂等冲突时存在。
    failure_code: str | None = Field(default=None, max_length=100)
    # message 是可直接展示的安全确定性文案。
    message: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_result_consistency(self) -> "ReturnRequestResult":
        """保证成功/失败、创建/重放和可选字段彼此一致。"""

        # 成功结果必须有申请编号和状态，且不能同时携带失败码。
        if self.success:
            # 任一字段矛盾都说明工具实现或恢复数据不可信。
            if (
                self.return_request_id is None
                or self.status is None
                or self.failure_code is not None
                or self.created == self.idempotent_replay
            ):
                # created 与 replay 必须恰好一个为 True。
                raise ValueError("成功结果必须包含编号/状态，且 created 与 replay 必须互斥")
            # 返回完成校验的成功对象。
            return self
        # 失败结果不能声称创建或重放成功，也不能携带申请编号与状态。
        if (
            self.created
            or self.idempotent_replay
            or self.return_request_id is not None
            or self.status is not None
            or self.outbox_event_id is not None
            or self.failure_code is None
        ):
            # 在状态进入 LangGraph 前拒绝矛盾结果。
            raise ValueError("失败结果必须只有 failure_code，不能包含成功字段")
        # 返回完成校验的失败对象。
        return self
