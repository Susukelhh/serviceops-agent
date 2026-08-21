"""只有人工批准后才允许调用的幂等退货申请写工具。"""

# BaseTool/tool 创建 LangChain Tool，执行节点仍会独立控制是否允许调用。
from langchain.tools import BaseTool, tool

# BaseModel/Field 在进入仓库前校验订单、原因和幂等键。
from pydantic import BaseModel, Field, field_validator

# 结构化工具结果和提交状态用于稳定响应。
from serviceops_agent.domain.outbox import (
    ReturnOutboxMetadata,
    build_return_outbox_event_id,
)
from serviceops_agent.domain.returns import ReturnRequestResult

# 仓库协议与有限业务异常隔离存储实现。
from serviceops_agent.infrastructure.return_repository import (
    ReturnIdempotencyConflictError,
    ReturnOrderNotEligibleError,
    ReturnOrderUnavailableError,
    ReturnRequestRepository,
)


class CreateReturnRequestInput(BaseModel):
    """审批通过后服务端传给写工具的有限参数。"""

    # order_id 格式与订单工具保持一致。
    order_id: str = Field(pattern=r"^SO\d{6}$")
    # reason 必须是用户在审批草案中明确提供的原因。
    reason: str = Field(min_length=5, max_length=500)
    # idempotency_key 支持恢复重试返回相同业务记录。
    idempotency_key: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )

    @field_validator("order_id", mode="before")
    @classmethod
    def normalize_order_id(cls, value: object) -> object:
        """在正则检查前统一订单号大小写与空白。"""

        # 只有字符串执行转换，其他类型交给 Pydantic 报错。
        if isinstance(value, str):
            # strip/upper 与只读订单工具保持一致。
            return value.strip().upper()
        # 返回原值供标准类型校验。
        return value


def create_return_request_tool(
    *,
    user_id: str,
    repository: ReturnRequestRepository,
    outbox_metadata: ReturnOutboxMetadata | None = None,
) -> BaseTool:
    """为当前可信用户创建身份绑定的退货申请写工具。"""

    # args_schema 不包含 user_id，审批恢复值和模型都不能伪造目标身份。
    @tool("create_return_request", args_schema=CreateReturnRequestInput)
    def create_return_request(
        order_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """为当前登录用户自己的已签收订单创建幂等退货申请。"""

        try:
            # 仓库在同一边界完成归属、状态资格和幂等原子检查。
            record, is_replay = repository.create_or_get(
                # 身份来自系统闭包而非 Tool Schema。
                user_id=user_id,
                # 参数已经通过 Pydantic 格式校验。
                order_id=order_id,
                # 原因来自已审批草案。
                reason=reason,
                # 幂等键关联原始 API 请求。
                idempotency_key=idempotency_key,
                # 可信审批上下文被闭包绑定，不会出现在模型可见的 args_schema 中。
                outbox_metadata=outbox_metadata,
            )
        # 不存在和越权共享同一安全结果，防止枚举其他用户订单。
        except ReturnOrderUnavailableError:
            # 构造结构化业务失败结果而不是抛出敏感仓库异常。
            result = ReturnRequestResult(
                # 本次没有写入或既有记录。
                success=False,
                # 没有新创建。
                created=False,
                # 也不是合法幂等重放。
                idempotent_replay=False,
                # 回显已校验订单号。
                order_id=order_id,
                # 有限失败码不区分不存在和越权。
                failure_code="order_unavailable",
                # 对外统一文案。
                message="未找到该订单，或该订单不属于当前用户，未创建退货申请。",
            )
            # 返回 JSON 基础类型字典。
            return result.model_dump(mode="json")
        # 本人订单未签收等状态不满足当前申请条件。
        except ReturnOrderNotEligibleError:
            # 构造资格拒绝结果。
            result = ReturnRequestResult(
                # 没有成功创建。
                success=False,
                # 没有新记录。
                created=False,
                # 不是幂等重放。
                idempotent_replay=False,
                # 回显本人订单号。
                order_id=order_id,
                # 稳定业务失败码。
                failure_code="order_not_eligible",
                # 不承诺具体售后政策，只说明当前演示状态规则。
                message="当前订单状态暂不支持创建退货申请。",
            )
            # 返回稳定字典。
            return result.model_dump(mode="json")
        # 相同幂等键用于不同负载时拒绝覆盖已有业务记录。
        except ReturnIdempotencyConflictError:
            # 构造幂等冲突结果。
            result = ReturnRequestResult(
                # 冲突不是成功重放。
                success=False,
                # 没有创建新记录。
                created=False,
                # 不返回旧记录，避免混淆不同业务负载。
                idempotent_replay=False,
                # 回显当前订单号。
                order_id=order_id,
                # 稳定冲突码供告警和客户端处理。
                failure_code="idempotency_conflict",
                # 不暴露原键对应的其他负载。
                message="幂等键已用于其他申请内容，未创建退货申请。",
            )
            # 返回结构化失败。
            return result.model_dump(mode="json")

        # 相同负载重放返回原申请号，不再次创建业务记录。
        if is_replay:
            # 构造成功重放结果。
            result = ReturnRequestResult(
                # 既有记录可以作为成功响应。
                success=True,
                # 本次没有新创建。
                created=False,
                # 明确标记幂等重放。
                idempotent_replay=True,
                # 返回原记录订单号。
                order_id=record.order_id,
                # 返回同一个稳定申请编号。
                return_request_id=record.return_request_id,
                # 返回原记录状态。
                status=record.status,
                # 生产 API 重放仍关联同一线程稳定 Outbox 事件。
                outbox_event_id=(
                    build_return_outbox_event_id(outbox_metadata.thread_id)
                    if outbox_metadata is not None
                    else None
                ),
                # 文案明确没有重复创建。
                message=f"退货申请 {record.return_request_id} 已存在，本次未重复创建。",
            )
            # 返回 JSON 友好结构。
            return result.model_dump(mode="json")

        # 首次写入成功时构造新建结果。
        result = ReturnRequestResult(
            # 仓库已经原子写入记录。
            success=True,
            # 标记本次创建。
            created=True,
            # 首次创建不是重放。
            idempotent_replay=False,
            # 返回目标订单号。
            order_id=record.order_id,
            # 返回新申请编号。
            return_request_id=record.return_request_id,
            # 返回 submitted 状态。
            status=record.status,
            # 事件 ID 由可信 thread_id 生成；直接图教学调用可以没有该字段。
            outbox_event_id=(
                build_return_outbox_event_id(outbox_metadata.thread_id)
                if outbox_metadata is not None
                else None
            ),
            # 使用确定性成功文案。
            message=f"退货申请 {record.return_request_id} 已提交。",
        )
        # 工具始终返回普通 JSON 字典，不暴露仓库对象。
        return result.model_dump(mode="json")

    # 返回绑定可信身份和仓库的写工具。
    return create_return_request
