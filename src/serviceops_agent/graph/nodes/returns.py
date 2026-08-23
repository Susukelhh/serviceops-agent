"""退货写操作的草案准备、人工中断、批准执行和拒绝响应节点。"""

# logging 记录脱敏写工具异常；re 提取订单号和用户明确提供的原因。
import logging
import re

# Callable 标注绑定仓库的同步节点工厂返回值。
from collections.abc import Callable

# interrupt 首次暂停图，恢复时返回 Command(resume=...) 提供的审批值。
from langgraph.types import interrupt

# 审批草案和备注摘要进入 Outbox，原始原因/备注不会被复制到审计事件表。
from serviceops_agent.domain.audit import build_comment_digest, build_proposal_digest

# AgentAction 不适用本固定写流程；订单状态用于审批前资格检查。
from serviceops_agent.domain.orders import OrderStatus

# ReturnOutboxMetadata 只由可信审批上下文构造，并通过工具闭包传入仓库事务。
from serviceops_agent.domain.outbox import ReturnOutboxMetadata

# 审批、草案、工具结果和流程状态是节点间稳定边界。
from serviceops_agent.domain.returns import (
    ApprovalDecision,
    ApprovalRequestPayload,
    ReturnRequestProposal,
    ReturnRequestResult,
    ReturnWorkflowStatus,
)

# ServiceState 保存可信身份、幂等键、审批决定和最终申请编号。
from serviceops_agent.graph.state import ServiceState

# 订单仓库用于审批前只读检查；退货仓库只在批准后写入。
from serviceops_agent.infrastructure.order_repository import OrderRepository
from serviceops_agent.infrastructure.return_repository import ReturnRequestRepository

# 写工具由执行节点创建，并把可信 user_id 绑定在模型不可见闭包中。
from serviceops_agent.tools.return_tools import create_return_request_tool

# 订单号格式与其他订单路径保持一致。
RETURN_ORDER_ID_PATTERN = re.compile(r"\bSO\d{6}\b", flags=re.IGNORECASE)
# 原因必须使用“原因：”或“理由：”明确分隔，避免系统自行猜测用户意图。
RETURN_REASON_PATTERN = re.compile(r"(?:原因|理由)\s*[：:]\s*(?P<reason>.+)$")
# 用户也可能用“因为/由于/但是”明确表达原因；只截取到下一个标点，避免吞入后续指令。
RETURN_CAUSAL_REASON_PATTERN = re.compile(
    r"(?:因为|由于|但是|但|不过)\s*(?P<reason>[^，,。！？!?]{5,80})(?:[，,。！？!?]|$)"
)

# 模块 Logger 不记录审批备注、退货原因、工具结果或异常正文。
logger = logging.getLogger(__name__)

# StateUpdate 表示节点只返回自己负责的部分状态字段。
type StateUpdate = dict[str, object]
# ReturnNode 是可以注册到 LangGraph 的同步节点签名。
type ReturnNode = Callable[[ServiceState], StateUpdate]


def extract_explicit_return_reason(message: str) -> str | None:
    """从带原因标签或明确因果连接词的退货请求中提取用户原话。"""

    # “原因：...”是最明确、向后兼容的第一优先级格式。
    labeled_match = RETURN_REASON_PATTERN.search(message)
    if labeled_match is not None:
        # strip 只去掉边界空白，不改写用户陈述。
        return labeled_match.group("reason").strip()
    # 没有标签时，只接受“因为/由于/但”等明确因果语法，不从任意剩余文本猜原因。
    causal_match = RETURN_CAUSAL_REASON_PATTERN.search(message)
    if causal_match is None:
        return None
    # 返回标点前的有限原文，仍由后续长度和 Pydantic 约束复核。
    return causal_match.group("reason").strip()


def create_return_request_proposal_node(
    order_repository: OrderRepository,
) -> ReturnNode:
    """创建执行订单归属/状态预检查并生成审批草案的节点。"""

    def prepare_return_request(state: ServiceState) -> StateUpdate:
        """从规范化问题提取明确参数，未通过前置条件时绝不进入审批。"""

        # 使用规范化文本，避免首尾和连续空白影响正则提取。
        message = state.get("normalized_message", "")
        # 查找文本中的第一个合法订单号；当前一次退货流程只处理一个订单。
        order_match = RETURN_ORDER_ID_PATTERN.search(message)
        # 缺少订单号时向用户澄清，不创建审批任务。
        if order_match is None:
            # 返回可以直接结束本轮图的澄清状态。
            return {
                # 明确同时说明订单号和原因格式。
                "answer": (
                    "请提供退货订单号和原因，格式例如："
                    "为订单 SO100002 申请退货，原因：商品不合适。"
                ),
                # 当前不需要审批人介入。
                "approval_required": False,
                # 用户补充参数后仍可自动继续。
                "requires_human": False,
                # API 提示继续收集信息。
                "needs_clarification": True,
                # 条件边把 clarification 映射到 END。
                "return_workflow_status": ReturnWorkflowStatus.CLARIFICATION.value,
                # 保存明确停止原因。
                "agent_stop_reason": "return_request_needs_order_id",
                # 事件用于统计写操作参数完整率。
                "events": ["graph:return_request_order_id_required"],
            }

        # 规范化为大写，保持仓库、工具和审批负载一致。
        order_id = order_match.group(0).upper()
        # 原因必须由标签或明确因果连接词表达，不能从任意剩余文本猜测。
        reason = extract_explicit_return_reason(message)
        # 没有明确原因时同样不创建审批任务。
        if reason is None:
            # 返回原因澄清状态。
            return {
                # 回显目标订单号，方便用户补充。
                "answer": f"请补充订单 {order_id} 的退货原因，格式例如：原因：商品不合适。",
                # 尚无可审批完整草案。
                "approval_required": False,
                # 用户可以在下一轮补充原因。
                "requires_human": False,
                # 标记需要澄清。
                "needs_clarification": True,
                # 流程停在参数收集阶段。
                "return_workflow_status": ReturnWorkflowStatus.CLARIFICATION.value,
                # 保存停止原因。
                "agent_stop_reason": "return_request_needs_reason",
                # 事件区分缺原因与缺订单号。
                "events": ["graph:return_request_reason_required"],
            }

        # 原因不足五个字符时不产生含糊草案。
        if len(reason) < 5:
            # 返回更具体的澄清提示。
            return {
                # 要求提供可供审批人理解的完整原因。
                "answer": f"订单 {order_id} 的退货原因过短，请补充更具体的说明。",
                # 不进入审批。
                "approval_required": False,
                # 当前无需人工审批。
                "requires_human": False,
                # 用户需要补充信息。
                "needs_clarification": True,
                # 保持澄清状态。
                "return_workflow_status": ReturnWorkflowStatus.CLARIFICATION.value,
                # 记录停止原因。
                "agent_stop_reason": "return_request_reason_too_short",
                # 事件不包含用户原因正文。
                "events": ["graph:return_request_reason_too_short"],
            }

        # user_id 必须来自系统 State；自然语言中的身份声明不会被读取。
        user_id = state.get("user_id", "")
        # 缺少可信身份时禁止查询或写入订单数据。
        if not user_id:
            # 返回安全人工状态。
            return {
                # 固定文案不鼓励用户自行填写系统身份。
                "answer": "当前无法验证用户身份，不能发起退货申请，请联系人工客服。",
                # 没有审批任务。
                "approval_required": False,
                # 身份边界异常需要人工处理。
                "requires_human": True,
                # 不是普通文本参数不足。
                "needs_clarification": False,
                # 标记流程失败。
                "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
                # 复用稳定身份错误码。
                "agent_failure_code": "missing_identity",
                # 保存停止原因。
                "agent_stop_reason": "missing_identity",
                # 事件记录审批前身份阻断。
                "events": ["graph:return_request_missing_identity_blocked"],
            }

        # 审批前只读查询本人订单，避免为不存在或越权目标创建人工任务。
        order = order_repository.get_for_user(order_id=order_id, user_id=user_id)
        # 不存在和不属于当前用户保持相同响应。
        if order is None:
            # 返回不创建审批任务的安全澄清状态。
            return {
                # 合并不存在和无权限，防止订单枚举。
                "answer": "未找到该订单，或该订单不属于当前用户，无法发起退货申请。",
                # 没有合法审批目标。
                "approval_required": False,
                # 用户可以核对订单号。
                "requires_human": False,
                # 标记需要修正目标订单。
                "needs_clarification": True,
                # 流程按澄清结束。
                "return_workflow_status": ReturnWorkflowStatus.CLARIFICATION.value,
                # 保存停止原因。
                "agent_stop_reason": "return_order_unavailable",
                # 事件不区分不存在和越权。
                "events": ["graph:return_request_order_unavailable"],
            }

        # 当前业务演示只允许已签收订单进入审批。
        if order.status != OrderStatus.DELIVERED:
            # 运输中或未发货订单不会创建退货申请。
            return {
                # 使用确定性状态拒绝文案。
                "answer": "当前订单尚未签收，暂不支持创建退货申请。",
                # 没有可批准写操作。
                "approval_required": False,
                # 这是业务规则拒绝，不需要人工审批。
                "requires_human": False,
                # 用户不需要补充文本参数。
                "needs_clarification": False,
                # 流程明确标记 declined。
                "return_workflow_status": ReturnWorkflowStatus.DECLINED.value,
                # 保存停止原因。
                "agent_stop_reason": "return_order_not_eligible",
                # 事件支持统计资格拒绝率。
                "events": ["graph:return_request_order_not_eligible"],
            }

        # 幂等键由 API 写入 State；测试或非 HTTP 调用可使用 request_id 作为安全默认值。
        idempotency_key = state.get("idempotency_key") or state.get("request_id", "")
        # 构造强类型审批草案；非法幂等键会在 API 边界或 Pydantic 立即失败。
        proposal = ReturnRequestProposal(
            # 动作固定为当前唯一写工具。
            action="create_return_request",
            # 已通过本人订单和状态预检查。
            order_id=order_id,
            # 用户明确原因。
            reason=reason,
            # 重试必须保持相同键。
            idempotency_key=idempotency_key,
            # 标记写风险。
            risk_level="write",
        )
        # 返回将进入 interrupt 节点的完整草案状态。
        return {
            # 草案以 JSON 字典保存；恢复后的每个读取节点都会重新执行 Pydantic 校验。
            "return_request_proposal": proposal.model_dump(mode="json"),
            # API 看到中断时会把该值设置为 True。
            "approval_required": True,
            # 当前需要审批人参与，区别于普通自动完成。
            "requires_human": True,
            # 所有用户参数已经完整。
            "needs_clarification": False,
            # 条件边进入审批中断节点。
            "return_workflow_status": ReturnWorkflowStatus.APPROVAL_PENDING.value,
            # 停止原因会在真正 interrupt 后由 API 表达。
            "agent_stop_reason": "approval_pending",
            # 事件表明草案已经通过审批前校验。
            "events": ["graph:return_request_proposal_prepared"],
        }

    # 返回绑定只读订单仓库的节点。
    return prepare_return_request


def request_return_approval(state: ServiceState) -> StateUpdate:
    """暂停图并在恢复时校验人工审批决定。"""

    try:
        # Checkpoint 中只保存 JSON 字典，恢复后必须重新构造强类型草案。
        proposal = ReturnRequestProposal.model_validate(
            state.get("return_request_proposal")
        )
    # 防御图装配错误、快照损坏或不完整草案。
    except Exception:
        # 没有可信草案时不能创建 interrupt 或执行工具。
        return {
            # 标记流程失败。
            "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
            # 当前没有合法审批任务。
            "approval_required": False,
            # 需要人工排查状态。
            "requires_human": True,
            # 稳定错误码不包含状态原文。
            "agent_failure_code": "invalid_return_proposal",
            # 保存停止原因。
            "agent_stop_reason": "invalid_return_proposal",
            # 事件记录安全拒绝。
            "events": ["graph:return_approval_invalid_proposal_blocked"],
        }

    # 构造不会泄漏 user_id、幂等键或整段原始消息的审批负载。
    approval_payload = ApprovalRequestPayload(
        # 固定中断类型供 API 校验。
        kind="return_request_approval",
        # 请求 ID 用于日志关联。
        request_id=state.get("request_id", "unknown"),
        # 固定写动作。
        action=proposal.action,
        # 审批目标订单。
        order_id=proposal.order_id,
        # 用户明确原因必须供审批人审阅。
        reason=proposal.reason,
        # 写风险标签。
        risk_level=proposal.risk_level,
        # 固定说明批准后会发生的操作。
        message="批准后将为该用户的已签收订单创建一条退货申请记录。",
    )
    # 首次调用会暂停并把 payload 返回 API；恢复时返回 Command.resume 中的值。
    raw_decision = interrupt(approval_payload.model_dump(mode="json"))

    try:
        # 恢复值属于外部输入，必须重新通过审批 Schema 校验。
        decision = ApprovalDecision.model_validate(raw_decision)
    # 非字典、缺 reviewer_id 或备注过长等恢复值全部安全失败。
    except Exception as error:
        # 日志只记录异常类型和请求 ID，不记录审批原始输入。
        logger.warning(
            "退货审批恢复值校验失败: request_id=%s cause_type=%s",
            state.get("request_id", "unknown"),
            type(error).__name__,
        )
        # 返回人工错误状态，禁止进入写工具。
        return {
            # 没有通过审批 Schema。
            "approval_required": False,
            # 流程失败。
            "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
            # 需要人工通过正确接口重新处理。
            "requires_human": True,
            # 稳定恢复错误码。
            "agent_failure_code": "invalid_approval_decision",
            # 保存停止原因。
            "agent_stop_reason": "invalid_approval_decision",
            # 不包含原始恢复值。
            "events": ["graph:return_approval_invalid_decision_blocked"],
        }

    # approved 决定后续条件边，reviewer_id/comment 只保存于内部 State。
    status = (
        # 批准进入写工具路径。
        ReturnWorkflowStatus.APPROVED.value
        # 拒绝进入确定性拒绝响应。
        if decision.approved
        else ReturnWorkflowStatus.REJECTED.value
    )
    # 返回审批决定的状态增量。
    return {
        # 决定以 JSON 字典保存；执行器与路由会再次校验，避免自定义类反序列化依赖。
        "approval_decision": decision.model_dump(mode="json"),
        # 已收到决定，不再处于等待状态。
        "approval_required": False,
        # 流程阶段用于条件边选择执行或拒绝。
        "return_workflow_status": status,
        # 批准和拒绝都已完成人工输入，不需要额外人工接管。
        "requires_human": False,
        # 用户参数完整。
        "needs_clarification": False,
        # 事件只记录决定，不包含 reviewer_id 和 comment。
        "events": [
            "graph:return_request_approved"
            if decision.approved
            else "graph:return_request_rejected"
        ],
    }


def create_return_request_execution_node(
    repository: ReturnRequestRepository,
) -> ReturnNode:
    """创建只有批准状态才能调用幂等写工具的执行节点。"""

    def execute_return_request(state: ServiceState) -> StateUpdate:
        """复查审批、身份和草案后执行一次写工具。"""

        # 默认没有可信草案，只有完整通过两项 Schema 后才会替换。
        proposal: ReturnRequestProposal | None = None
        # 默认没有可信审批决定。
        decision: ApprovalDecision | None = None
        try:
            # 把 Checkpoint JSON 草案恢复为强类型领域模型。
            proposal = ReturnRequestProposal.model_validate(
                state.get("return_request_proposal")
            )
            # 审批决定也必须在写入前重新通过严格布尔 Schema。
            decision = ApprovalDecision.model_validate(state.get("approval_decision"))
        # 任一持久化对象异常都不能调用写工具。
        except Exception:
            # 清空两项，统一进入下面的纵深失败分支。
            proposal = None
            decision = None
        # 没有合法决定或未明确批准都不能调用写工具。
        if proposal is None or decision is None or decision.approved is not True:
            # 返回纵深审批边界失败状态。
            return {
                # 禁止写入。
                "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
                # 不再等待审批输入。
                "approval_required": False,
                # 状态异常需要人工排查。
                "requires_human": True,
                # 稳定错误码。
                "agent_failure_code": "write_without_approval",
                # 保存停止原因。
                "agent_stop_reason": "write_without_approval",
                # 事件证明执行器自己也检查了审批，而不是只依赖条件边。
                "events": ["graph:return_write_without_approval_blocked"],
            }

        # user_id 必须来自初始 API State，不能来自审批恢复值。
        user_id = state.get("user_id", "")
        # 缺失可信身份时禁止创建工具。
        if not user_id:
            # 返回身份安全错误。
            return {
                # 流程失败。
                "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
                # 当前不等待审批。
                "approval_required": False,
                # 需要人工排查。
                "requires_human": True,
                # 复用身份错误码。
                "agent_failure_code": "missing_identity",
                # 保存停止原因。
                "agent_stop_reason": "missing_identity",
                # 事件记录写入前身份阻断。
                "events": ["graph:return_write_missing_identity_blocked"],
            }

        try:
            # 直接 LangGraph 教学调用没有 HTTP/JWT 上下文，因此允许不生成 Outbox。
            outbox_metadata: ReturnOutboxMetadata | None = None
            # 生产 API 会同时注入 thread_id 和 token_jti，领域校验已经保证二者成对出现。
            if decision.thread_id is not None:
                # 这里的 assert 只帮助静态类型收窄；单独缺失会在 ApprovalDecision 阶段被拒绝。
                assert decision.token_jti is not None
                # 构造最小可信事件元数据，不包含退货原因、备注或幂等键原文。
                outbox_metadata = ReturnOutboxMetadata(
                    # 路径绑定的工作流线程。
                    thread_id=decision.thread_id,
                    # 初始 Checkpoint 中由 API 生成的请求标识。
                    request_id=state.get("request_id", ""),
                    # 审批主体来自 JWT sub。
                    actor_id=decision.reviewer_id,
                    # 只保存 JWT 唯一编号。
                    token_jti=decision.token_jti,
                    # 本节点只允许明确批准，类型固定为 True。
                    approved=True,
                    # 已审阅订单号。
                    order_id=proposal.order_id,
                    # 完整草案只保存不可逆摘要。
                    proposal_digest=build_proposal_digest(proposal),
                    # 自由文本备注也只保存规范化摘要。
                    comment_digest=build_comment_digest(decision.comment),
                )
            # 工具闭包同时绑定可信用户、仓库和系统 Outbox 元数据；args_schema 仍只有三个业务字段。
            write_tool = create_return_request_tool(
                user_id=user_id,
                repository=repository,
                outbox_metadata=outbox_metadata,
            )
            # 工具参数全部来自已审批草案，不读取新的模型输出或恢复值字段。
            raw_result = write_tool.invoke(
                {
                    # 已审批订单号。
                    "order_id": proposal.order_id,
                    # 已审批原因。
                    "reason": proposal.reason,
                    # 原始请求稳定幂等键。
                    "idempotency_key": proposal.idempotency_key,
                }
            )
            # 工具输出必须再次通过领域 Schema。
            result = ReturnRequestResult.model_validate(raw_result)
        # 未预期仓库异常、Tool 错误或结果校验错误统一安全转人工。
        except Exception as error:
            # 日志只记录类名和请求 ID，不记录审批内容或仓库异常正文。
            logger.warning(
                "退货写工具执行失败: request_id=%s cause_type=%s",
                state.get("request_id", "unknown"),
                type(error).__name__,
            )
            # 返回脱敏失败状态。
            return {
                # 固定用户文案。
                "answer": "退货申请服务暂时不可用，本次请求已建议转交人工客服。",
                # 写流程失败。
                "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
                # 不再等待审批。
                "approval_required": False,
                # 需要人工确认是否写入。
                "requires_human": True,
                # 系统故障不要求用户重复参数。
                "needs_clarification": False,
                # 稳定执行错误码。
                "agent_failure_code": "return_write_error",
                # 保存停止原因。
                "agent_stop_reason": "return_write_error",
                # 事件不包含异常正文。
                "events": ["graph:return_write_error_fallback_to_human"],
            }

        # 已知业务失败结果不抛异常，但仍不能标记流程完成。
        if not result.success:
            # 幂等冲突需要人工判断；订单状态变化等业务拒绝可以直接说明。
            requires_human = result.failure_code == "idempotency_conflict"
            # 返回工具提供的安全确定性文案。
            return {
                # 文案不区分不存在和越权。
                "answer": result.message,
                # 冲突视为失败，其他资格变化视为业务拒绝。
                "return_workflow_status": (
                    ReturnWorkflowStatus.FAILED.value
                    if requires_human
                    else ReturnWorkflowStatus.DECLINED.value
                ),
                # 审批已经结束。
                "approval_required": False,
                # 只有幂等冲突升级人工。
                "requires_human": requires_human,
                # 订单不可用时用户可以核对订单号。
                "needs_clarification": result.failure_code == "order_unavailable",
                # 保存有限失败码。
                "agent_failure_code": f"return_{result.failure_code}",
                # 保存停止原因。
                "agent_stop_reason": f"return_{result.failure_code}",
                # 保存实际写工具名用于审计。
                "tool_name": write_tool.name,
                # 保存经过校验的结构化失败结果。
                "tool_result": result.model_dump(mode="json"),
                # 事件不包含业务负载。
                "events": [f"graph:return_write_{result.failure_code}"],
            }

        # Pydantic 一致性校验保证成功时申请编号一定非空。
        assert result.return_request_id is not None
        # 首次写入和幂等重放使用不同事件，便于统计重试情况。
        write_event = (
            # 相同键重放没有新写入。
            "graph:return_request_idempotent_replay"
            # 首次批准执行创建新记录。
            if result.idempotent_replay
            else "graph:return_request_created"
        )
        # 返回成功或幂等成功的最终状态。
        return {
            # 使用工具确定性消息。
            "answer": result.message,
            # 保存稳定申请编号供 API 返回。
            "return_request_id": result.return_request_id,
            # 生产 API 路径保存稳定 Outbox ID，直接图学习路径允许为 None。
            "outbox_event_id": result.outbox_event_id,
            # 保存实际写工具名。
            "tool_name": write_tool.name,
            # 保存经过校验的工具结果。
            "tool_result": result.model_dump(mode="json"),
            # 写流程正常完成。
            "return_workflow_status": ReturnWorkflowStatus.COMPLETED.value,
            # 审批已经结束。
            "approval_required": False,
            # 不需要进一步人工。
            "requires_human": False,
            # 参数和写入均完整。
            "needs_clarification": False,
            # 明确停止原因。
            "agent_stop_reason": "return_request_completed",
            # 记录首次创建或幂等重放。
            "events": [write_event],
        }

    # 返回绑定退货写仓库的执行节点。
    return execute_return_request


def finalize_return_rejection(state: ServiceState) -> StateUpdate:
    """人工拒绝后生成零写入的确定性响应。"""

    try:
        # 读取并重建决定只用于防御性确认，不把 reviewer_id/comment 暴露给用户。
        decision = ApprovalDecision.model_validate(state.get("approval_decision"))
    # 如果节点被错误调用且没有合法决定，则统一进入安全失败。
    except Exception:
        # None 让下面的严格拒绝判断失败。
        decision = None
    # 没有明确 approved=False 时不能声称审批已拒绝。
    if decision is None or decision.approved is not False:
        # 返回错误状态并禁止任何写入。
        return {
            # 固定文案。
            "answer": "审批状态异常，未创建退货申请。",
            # 流程失败。
            "return_workflow_status": ReturnWorkflowStatus.FAILED.value,
            # 不再等待审批。
            "approval_required": False,
            # 需要人工排查审批状态。
            "requires_human": True,
            # 稳定错误码。
            "agent_failure_code": "invalid_rejection_state",
            # 停止原因。
            "agent_stop_reason": "invalid_rejection_state",
            # 事件记录纵深校验失败。
            "events": ["graph:return_rejection_invalid_state"],
        }

    # 正常拒绝不会调用 create_return_request 工具。
    return {
        # 清楚说明未产生写入。
        "answer": "审批未通过，未创建退货申请。",
        # 保持 rejected 终态。
        "return_workflow_status": ReturnWorkflowStatus.REJECTED.value,
        # 审批已经完成。
        "approval_required": False,
        # 不需要额外人工接管。
        "requires_human": False,
        # 用户参数并不缺失。
        "needs_clarification": False,
        # 保存拒绝停止原因。
        "agent_stop_reason": "return_request_rejected",
        # 事件证明流程在写工具之前结束。
        "events": ["graph:return_request_rejection_finalized"],
    }
