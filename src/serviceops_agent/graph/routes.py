"""状态图的条件路由函数。"""

# Literal 把可能返回的路由键限制为三个固定字符串，帮助类型检查发现拼写错误。
from typing import Literal

# Intent 提供有限业务意图，用枚举比较比直接比较任意字符串更安全。
from serviceops_agent.domain.enums import Intent

# ApprovalDecision/Proposal/WorkflowStatus 支持退货审批条件边的强类型检查。
from serviceops_agent.domain.returns import (
    ApprovalDecision,
    ReturnRequestProposal,
    ReturnWorkflowStatus,
)

# ServiceState 描述路由函数可读取的共享状态字段。
from serviceops_agent.graph.state import ServiceState


def _is_valid_return_proposal(raw_proposal: object) -> bool:
    """确认 Checkpoint 中的 JSON 草案仍符合退货领域 Schema。"""

    try:
        # model_validate 会检查固定动作、订单号、原因、幂等键和风险级别。
        ReturnRequestProposal.model_validate(raw_proposal)
    # 缺字段、类型错误或值越界都不能进入 interrupt 节点。
    except Exception:
        # False 会让路由采用人工安全默认路径。
        return False
    # 所有字段通过校验后才允许等待审批。
    return True


def select_response_path(
    state: ServiceState,
) -> Literal["faq", "order", "return_request", "human"]:
    """根据分类节点写入的意图选择下一条路径。

    未识别或缺失的意图默认转人工，而不是猜测答案。这是企业系统常用的“安全默认值”：
    宁可增加少量人工成本，也不让不确定的请求进入可能产生业务影响的自动路径。
    """

    # 从共享状态读取分类结果；使用 get 是因为 total=False 允许字段在早期阶段不存在。
    intent = state.get("intent")
    # FAQ 意图必须返回 `faq` 路由键，图构建器会再把它映射到 answer_faq 节点。
    if intent == Intent.FAQ:
        # 返回路由键而不是直接调用节点，保持“决策”和“执行”解耦。
        return "faq"
    # 订单意图必须返回 `order` 路由键，进入订单查询路径。
    if intent == Intent.ORDER_STATUS:
        # 返回固定字符串，确保与 builder.py 中的条件边映射表一致。
        return "order"
    # 明确退货申请属于需要 interrupt 审批的写操作路径。
    if intent == Intent.RETURN_REQUEST:
        # 路由只进入草案准备节点，不能直接执行写工具。
        return "return_request"
    # 任何未知值、人工意图或缺失字段都进入安全默认路径，避免系统猜测。
    return "human"


def select_faq_evidence_path(state: ServiceState) -> Literal["answer", "human"]:
    """根据检索证据门选择生成知识回答或安全转人工。"""

    # 只有显式 True 才允许回答；缺失、False 或其他异常值全部采用安全默认路径。
    if state.get("has_sufficient_evidence") is True and state.get("retrieval_hits"):
        # 返回 answer 路由键，图构建器会映射到 grounded FAQ 回答节点。
        return "answer"
    # 无证据、低分或检索故障全部进入人工接管，禁止依赖模型记忆猜测。
    return "human"


def select_faq_answer_path(state: ServiceState) -> Literal["complete", "human"]:
    """验证 FAQ 最终答案、grounding 标记和引用是否同时存在。"""

    # 三个条件必须同时满足：节点显式放行、存在非空答案、至少有一条合法引用。
    if (
        # 只有布尔 True 可以放行，缺失或其他真值对象不能绕过校验。
        state.get("faq_answer_grounded") is True
        # 空字符串或缺失答案不能作为成功响应结束图执行。
        and bool(state.get("answer"))
        # 至少一条由候选 RetrievalHit 确定性创建的 Citation 才算有依据。
        and bool(state.get("citations"))
    ):
        # complete 会在图构建器中映射到 END，不再执行其他响应节点。
        return "complete"
    # 任何字段缺失、生成拒答或引用越界都进入人工安全路径。
    return "human"


def select_order_plan_path(
    state: ServiceState,
) -> Literal["execute", "finalize", "clarify", "human"]:
    """把受约束规划动作映射为工具、汇总、澄清或人工节点。"""

    # next_action 只能由订单规划节点写入，缺失或未知值采用人工安全默认值。
    next_action = state.get("agent_next_action")
    # call_tool 必须同时存在强类型计划，执行器还会进行更严格的纵深校验。
    if next_action == "call_tool" and state.get("planned_tool_call") is not None:
        # execute 在图构建器中映射到唯一工具执行节点。
        return "execute"
    # finish 只有在至少存在一条执行记录时才允许进入最终汇总。
    if next_action == "finish" and state.get("tool_execution_records"):
        # finalize 会重新校验每条观察并生成确定性回答。
        return "finalize"
    # clarify 进入不调用工具的参数追问节点。
    if next_action == "clarify":
        # 澄清不是人工接管，用户补充订单号后可发起下一轮请求。
        return "clarify"
    # handoff、未知动作、空 finish 或状态矛盾全部进入人工安全路径。
    return "human"


def select_order_execution_path(state: ServiceState) -> Literal["continue", "human"]:
    """决定工具观察后返回规划器继续循环，还是立即安全转人工。"""

    # 只有执行器显式成功且写入 continue 时才允许形成回边。
    if (
        # 防止普通真值绕过布尔成功门。
        state.get("agent_execution_succeeded") is True
        # continue 是唯一允许回到规划节点的动作。
        and state.get("agent_next_action") == "continue"
    ):
        # 回到规划器，让它观察最新结果并选择下一个工具或 finish。
        return "continue"
    # 工具异常、非法计划、重复调用、身份缺失和步数超限都转人工。
    return "human"


def select_return_proposal_path(state: ServiceState) -> Literal["approval", "complete", "human"]:
    """决定退货草案进入审批中断、直接结束还是异常人工路径。"""

    # 只有显式待审批状态和强类型草案同时存在，才能调用 interrupt。
    if (
        # 状态必须由草案节点写入有限枚举。
        state.get("return_workflow_status") == ReturnWorkflowStatus.APPROVAL_PENDING.value
        # Checkpoint 草案虽是 JSON 字典，但必须仍能通过领域 Schema。
        and _is_valid_return_proposal(state.get("return_request_proposal"))
    ):
        # approval 映射到 request_return_approval 节点。
        return "approval"
    # 澄清和确定性业务拒绝已经包含最终 answer，可以安全结束。
    if state.get("return_workflow_status") in {
        # 参数/订单不可用时等待下一轮用户输入。
        ReturnWorkflowStatus.CLARIFICATION.value,
        # 未签收等确定性条件不满足时直接拒绝。
        ReturnWorkflowStatus.DECLINED.value,
    } and bool(state.get("answer")):
        # complete 映射到 END，不会创建审批或执行工具。
        return "complete"
    # 身份缺失、状态矛盾或节点异常采用人工安全默认路径。
    return "human"


def select_return_approval_path(state: ServiceState) -> Literal["execute", "reject", "human"]:
    """根据恢复后审批决定选择写工具、拒绝终点或异常人工路径。"""

    try:
        # Checkpoint 只保存 JSON 字典，路由时重新执行严格审批 Schema 校验。
        decision = ApprovalDecision.model_validate(state.get("approval_decision"))
    # 缺失或损坏决定采用人工安全默认路径。
    except Exception:
        # None 使批准和拒绝两个条件都无法通过。
        decision = None
    # 明确批准且流程状态一致时进入唯一写工具节点。
    if (
        decision is not None
        and decision.approved is True
        and state.get("return_workflow_status") == ReturnWorkflowStatus.APPROVED.value
    ):
        # execute 映射到 execute_return_request。
        return "execute"
    # 明确拒绝且状态一致时进入零写入拒绝响应节点。
    if (
        decision is not None
        and decision.approved is False
        and state.get("return_workflow_status") == ReturnWorkflowStatus.REJECTED.value
    ):
        # reject 不会触达写工具。
        return "reject"
    # 无决定、恢复值无效或状态矛盾全部转人工。
    return "human"
