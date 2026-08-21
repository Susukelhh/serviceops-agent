"""订单 Agent 的初始化、规划、工具执行、观察汇总和澄清节点。"""

# hashlib 为工具调用生成稳定指纹；json 将参数规范化后再计算摘要。
import hashlib
import json

# logging 只记录脱敏的工具故障类型和 request_id。
import logging

# Awaitable/Callable 精确描述同步与异步 LangGraph 节点闭包。
from collections.abc import Awaitable, Callable

# ToolPlanner 隔离确定性基线、真实千问规划器和测试替身。
from serviceops_agent.agent.planner import ToolPlanner

# AgentAction/ToolCallPlan/ToolExecutionRecord 是循环计划与观察的强类型边界。
from serviceops_agent.domain.agent import AgentAction, ToolCallPlan, ToolExecutionRecord

# OrderLookupResult 二次校验 LangChain Tool 的实际返回结构。
from serviceops_agent.domain.orders import OrderLookupResult

# ServiceState 是所有订单循环节点共享和增量更新的状态。
from serviceops_agent.graph.state import ServiceState

# 仓库协议支持默认 JSON 仓库和故障/隔离测试替身。
from serviceops_agent.infrastructure.order_repository import OrderRepository

# LLMServiceError 是真实规划模型已经归一化后的有限故障。
from serviceops_agent.llm.errors import LLMServiceError

# 工具工厂把系统 user_id 绑定到只读订单查询 Tool 中。
from serviceops_agent.tools.order_tools import create_order_status_tool

# 模块 Logger 不会记录用户问题、工具参数、工具结果或异常正文。
logger = logging.getLogger(__name__)

# StateUpdate 是 LangGraph 节点返回并合并到共享 State 的部分字段。
type StateUpdate = dict[str, object]
# SyncOrderNode 表示不访问远程模型的同步节点。
type SyncOrderNode = Callable[[ServiceState], StateUpdate]
# AsyncOrderNode 表示需要等待规划器的异步节点。
type AsyncOrderNode = Callable[[ServiceState], Awaitable[StateUpdate]]


def create_tool_call_fingerprint(tool_name: str, arguments: dict[str, str]) -> str:
    """根据工具名和规范化参数生成不包含明文参数的稳定 SHA-256 指纹。"""

    # sort_keys 保证参数字典插入顺序不同也得到相同序列化文本。
    canonical_payload = json.dumps(
        # 同时包含工具名和参数，避免不同工具的相同参数产生相同指纹。
        {"tool_name": tool_name, "arguments": arguments},
        # 键排序形成稳定规范表示。
        sort_keys=True,
        # 紧凑分隔符去除与语义无关的空格。
        separators=(",", ":"),
        # 保留中文参数，跨平台 UTF-8 编码仍然稳定。
        ensure_ascii=False,
    )
    # SHA-256 只用于请求内去重标识，不用于密码存储或身份认证。
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _validated_history(state: ServiceState) -> list[ToolExecutionRecord]:
    """从 State 中只取运行时类型正确的工具执行记录。"""

    # total=False State 在初始化前可能没有该字段，因此使用空列表默认值。
    raw_history = state.get("tool_execution_records", [])
    # 未知对象不能进入规划上下文或回答汇总。
    return [record for record in raw_history if isinstance(record, ToolExecutionRecord)]


def initialize_order_agent(state: ServiceState) -> StateUpdate:
    """为订单工具循环建立请求级计数、历史和停止状态。"""

    # 初始化节点不需要读取业务字段，但显式引用 State 表明签名与其他节点一致。
    _ = state
    # 每次新图执行都从干净的请求级 Agent 状态开始。
    return {
        # 尚未实际执行任何工具。
        "tool_call_count": 0,
        # 没有执行记录时规划器会从用户问题选择第一个动作。
        "tool_execution_records": [],
        # 指纹列表用于执行器在调用前拦截相同工具和参数。
        "tool_call_fingerprints": [],
        # 多订单查询结果将按执行顺序累积到该列表。
        "queried_order_ids": [],
        # 初始化完成后固定进入 planning 状态。
        "agent_next_action": "planning",
        # 尚未通过或失败任何工具执行。
        "agent_execution_succeeded": False,
        # 事件让 Trace 明确显示循环边界从哪里开始。
        "events": ["graph:order_agent_initialized"],
    }


def create_order_planning_node(
    planner: ToolPlanner,
    *,
    max_tool_steps: int,
) -> AsyncOrderNode:
    """创建绑定规划器和最大工具步数的异步规划节点。"""

    async def plan_order_action(state: ServiceState) -> StateUpdate:
        """根据问题和工具观察选择调用、完成、澄清或人工动作。"""

        # 已执行次数只由执行器写入，规划模型不能修改该安全计数。
        tool_call_count = state.get("tool_call_count", 0)
        # 防御异常 State 类型，非整数按达到上限处理而不是放宽限制。
        safe_tool_call_count = (
            tool_call_count if isinstance(tool_call_count, int) else max_tool_steps
        )
        # 只向规划器提供通过 ToolExecutionRecord 校验的历史。
        history = _validated_history(state)
        # 规划器读取规范化问题，与分类和检索使用同一文本版本。
        user_message = state.get("normalized_message", "")
        # 请求标识只用于脱敏日志关联。
        request_id = state.get("request_id", "unknown")

        try:
            # 规划器可能是离线基线、真实千问结构化模型或测试替身。
            plan = await planner.plan(user_message=user_message, history=history)
        # 真实模型认证、限流、超时和结构化输出故障进入稳定人工路径。
        except LLMServiceError as error:
            # 日志只记录有限错误类别和可重试标记。
            logger.warning(
                "订单工具规划失败并降级到人工: request_id=%s kind=%s retryable=%s",
                request_id,
                error.kind.value,
                error.retryable,
            )
            # 返回可由条件边路由的安全失败状态。
            return {
                # 不保留上轮计划，避免执行器误用陈旧动作。
                "planned_tool_call": None,
                # human 映射到统一人工接管节点。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定内部码不包含供应商响应正文。
                "agent_failure_code": f"planner_{error.kind.value}",
                # 明确说明循环停止阶段。
                "agent_stop_reason": "planner_failure",
                # 自动流程未完成，需要人工处理。
                "requires_human": True,
                # 系统故障不是用户缺参数。
                "needs_clarification": False,
                # 事件只包含有限错误类别。
                "events": [f"graph:order_planner_{error.kind.value}_fallback_to_human"],
            }
        # 自定义规划器编程错误同样不能击穿 FastAPI，但不会伪装成外部模型错误。
        except Exception as error:
            # 只记录异常类名，不记录可能包含用户文本的 message。
            logger.warning(
                "订单规划器内部异常并降级到人工: request_id=%s cause_type=%s",
                request_id,
                type(error).__name__,
            )
            # 返回厂商无关的内部规划故障状态。
            return {
                # 清空可能存在的旧计划。
                "planned_tool_call": None,
                # 安全默认路由为人工。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 有限错误类别支持告警聚合。
                "agent_failure_code": "planner_internal_error",
                # 停止原因区分模型错误和本地实现错误。
                "agent_stop_reason": "planner_failure",
                # 当前请求需要人工完成。
                "requires_human": True,
                # 不要求用户反复补充参数。
                "needs_clarification": False,
                # 不在事件中拼接异常正文。
                "events": ["graph:order_planner_internal_error_fallback_to_human"],
            }

        # 防御不遵循 Protocol 的运行时替身返回普通字典或 None。
        if not isinstance(plan, ToolCallPlan):
            # 无效计划不能进入执行器。
            return {
                # 清空旧计划。
                "planned_tool_call": None,
                # 进入人工安全路径。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定错误码用于测试和监控。
                "agent_failure_code": "planner_invalid_result",
                # 记录停止阶段。
                "agent_stop_reason": "planner_failure",
                # 需要人工介入。
                "requires_human": True,
                # 不是普通澄清。
                "needs_clarification": False,
                # 事件不包含实际无效返回值。
                "events": ["graph:order_planner_invalid_result_blocked"],
            }

        # 最大步数在执行任何新工具之前检查，避免第 N+1 次调用已经发生后才停止。
        if plan.action == AgentAction.CALL_TOOL and safe_tool_call_count >= max_tool_steps:
            # 丢弃超过预算的工具计划并转人工。
            return {
                # 不允许执行本轮计划。
                "planned_tool_call": None,
                # 条件边进入人工节点。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定码用于计算循环预算耗尽率。
                "agent_failure_code": "max_tool_steps_exceeded",
                # 停止原因直接表达预算边界。
                "agent_stop_reason": "max_tool_steps_exceeded",
                # 未能安全完成全部任务。
                "requires_human": True,
                # 用户补充文字不能扩大本请求工具预算。
                "needs_clarification": False,
                # 事件证明第 N+1 次工具调用被提前拦截。
                "events": ["graph:order_agent_max_tool_steps_blocked"],
            }

        # 调用计划写入 State，执行器还会检查名称、参数、身份、指纹和 Tool Schema。
        if plan.action == AgentAction.CALL_TOOL:
            # 返回待执行计划和清晰轨迹事件。
            return {
                # 保存强类型计划，不把它直接视为可信工具调用。
                "planned_tool_call": plan,
                # 条件边根据 call_tool 进入执行器。
                "agent_next_action": AgentAction.CALL_TOOL.value,
                # 规划阶段尚未要求人工。
                "requires_human": False,
                # 工具参数当前结构完整。
                "needs_clarification": False,
                # 不记录具体订单号，避免教学事件轨迹泄漏业务参数。
                "events": ["graph:order_agent_planned_tool_call"],
            }

        # 没有任何观察结果却要求 finish，通常表示模型过早停止；安全改为澄清。
        if plan.action == AgentAction.FINISH and not history:
            # 不能让空结果进入回答汇总节点。
            return {
                # 非调用动作不保存工具计划。
                "planned_tool_call": None,
                # 进入参数澄清节点。
                "agent_next_action": AgentAction.CLARIFY.value,
                # 记录没有可汇总结果的停止原因。
                "agent_stop_reason": "finish_without_observation",
                # 澄清仍属于可自动继续场景。
                "requires_human": False,
                # API 提示前端继续收集信息。
                "needs_clarification": True,
                # 事件帮助评测规划器过早停止率。
                "events": ["graph:order_agent_premature_finish_redirected_to_clarify"],
            }

        # 正常 finish 表示至少已经有一条工具观察，可以进入确定性汇总节点。
        if plan.action == AgentAction.FINISH:
            # 返回明确停止状态。
            return {
                # 清空最后一次调用计划，保证结束态没有待执行动作。
                "planned_tool_call": None,
                # 条件边映射到 finalize_order_response。
                "agent_next_action": AgentAction.FINISH.value,
                # 保存规划器停止原因而不保存思维链。
                "agent_stop_reason": "planner_finished",
                # 正常完成无需人工。
                "requires_human": False,
                # 是否需要核对订单号由最终工具结果决定。
                "needs_clarification": False,
                # 轨迹明确显示循环为什么停止。
                "events": ["graph:order_agent_planned_finish"],
            }

        # clarify 表示用户没有提供合法订单号等必要参数。
        if plan.action == AgentAction.CLARIFY:
            # 返回澄清路由状态。
            return {
                # 不存在待执行计划。
                "planned_tool_call": None,
                # 条件边映射到 clarification 响应节点。
                "agent_next_action": AgentAction.CLARIFY.value,
                # 记录循环停止在等待用户输入。
                "agent_stop_reason": "needs_clarification",
                # 不需要人工客服。
                "requires_human": False,
                # API 调用方据此展示补充信息输入。
                "needs_clarification": True,
                # 规划和实际澄清响应使用不同事件，便于观察节点顺序。
                "events": ["graph:order_agent_planned_clarification"],
            }

        # 剩余合法枚举只能是 HANDOFF，使用安全人工路径结束。
        return {
            # 人工动作不应携带待执行计划。
            "planned_tool_call": None,
            # 条件边映射到统一人工节点。
            "agent_next_action": AgentAction.HANDOFF.value,
            # 标记为规划器主动拒绝自动处理。
            "agent_failure_code": "planner_requested_handoff",
            # 保存明确停止原因。
            "agent_stop_reason": "planner_requested_handoff",
            # 自动处理未完成。
            "requires_human": True,
            # 不要求用户继续补参数。
            "needs_clarification": False,
            # 事件不包含模型 reason 正文。
            "events": ["graph:order_agent_planned_handoff"],
        }

    # 返回已经绑定规划器和最大步数的异步节点。
    return plan_order_action


def create_order_tool_execution_node(
    repository: OrderRepository,
    *,
    max_tool_steps: int,
) -> SyncOrderNode:
    """创建执行唯一白名单订单工具并写入观察历史的节点。"""

    def execute_order_tool(state: ServiceState) -> StateUpdate:
        """在执行前检查计划、白名单、身份、步数与重复指纹。"""

        # 规划节点应写入强类型 ToolCallPlan；任何其他对象都不能调用工具。
        plan = state.get("planned_tool_call")
        # 请求标识仅用于脱敏故障日志。
        request_id = state.get("request_id", "unknown")
        # 已验证历史用于追加观察和重复调用判断。
        history = _validated_history(state)
        # 读取已执行次数，异常类型采用达到上限的安全默认值。
        raw_tool_call_count = state.get("tool_call_count", 0)
        # 只有真正整数可以继续比较预算。
        tool_call_count = (
            raw_tool_call_count if isinstance(raw_tool_call_count, int) else max_tool_steps
        )

        # 执行器不相信路由一定正确，独立确认计划类型和动作。
        if not isinstance(plan, ToolCallPlan) or plan.action != AgentAction.CALL_TOOL:
            # 阻止空计划、旧计划或非调用计划触发工具。
            return {
                # 执行没有成功。
                "agent_execution_succeeded": False,
                # 下一跳进入人工。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 有限故障码便于发现图装配问题。
                "agent_failure_code": "invalid_tool_plan",
                # 停止原因说明执行边界拒绝了计划。
                "agent_stop_reason": "invalid_tool_plan",
                # 需要人工完成当前任务。
                "requires_human": True,
                # 不是用户参数不足。
                "needs_clarification": False,
                # 事件不包含无效计划内容。
                "events": ["graph:order_tool_invalid_plan_blocked"],
            }

        # 即使规划节点已经检查，执行器仍在真实调用前独立保护最大步数。
        if tool_call_count >= max_tool_steps:
            # 拒绝超过请求预算的工具调用。
            return {
                # 没有发生真实执行。
                "agent_execution_succeeded": False,
                # 转人工而不是形成无界循环。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 与规划节点使用相同稳定码。
                "agent_failure_code": "max_tool_steps_exceeded",
                # 保存停止原因。
                "agent_stop_reason": "max_tool_steps_exceeded",
                # 需要人工处理剩余任务。
                "requires_human": True,
                # 用户补文字不会重置当前请求预算。
                "needs_clarification": False,
                # 事件证明执行器的纵深防御生效。
                "events": ["graph:order_tool_max_steps_blocked"],
            }

        # 当前白名单只允许只读订单查询；任何模型编造名称都直接拒绝。
        if plan.tool_name != "get_order_status":
            # 不尝试模糊匹配或自动纠正未知工具名。
            return {
                # 工具没有执行。
                "agent_execution_succeeded": False,
                # 进入人工安全路径。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 统计规划器越权建议率。
                "agent_failure_code": "tool_not_allowed",
                # 停止原因明确为白名单拒绝。
                "agent_stop_reason": "tool_not_allowed",
                # 当前任务需要人工判断。
                "requires_human": True,
                # 不要求用户修改订单号来绕过工具边界。
                "needs_clarification": False,
                # 事件不包含模型编造的工具名。
                "events": ["graph:order_tool_not_allowed_blocked"],
            }

        # user_id 必须来自可信 API State，绝不能从计划中的 order_id 推导或覆盖。
        user_id = state.get("user_id", "")
        # 缺失身份时禁止访问订单仓库。
        if not user_id:
            # 返回身份边界失败状态。
            return {
                # 工具没有执行。
                "agent_execution_succeeded": False,
                # 条件边进入人工节点。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定安全错误码供响应节点选择准确文案。
                "agent_failure_code": "missing_identity",
                # 记录停止原因。
                "agent_stop_reason": "missing_identity",
                # 缺少可信身份必须人工处理。
                "requires_human": True,
                # 不能让用户在自然语言里自行补充系统身份。
                "needs_clarification": False,
                # 轨迹记录安全拒绝。
                "events": ["graph:order_tool_missing_identity_blocked"],
            }

        # 执行参数由服务端方法从扁平强类型计划构造，模型不能提交额外字典字段。
        tool_arguments = plan.tool_arguments()
        # 指纹根据工具名和规范参数生成，不把明文参数写入日志或事件。
        fingerprint = create_tool_call_fingerprint(plan.tool_name, tool_arguments)
        # 合并专用列表和记录历史并保序去重，防御字段丢失且保证审计输出稳定。
        ordered_fingerprints = list(
            # dict.fromkeys 保留首次出现顺序。
            dict.fromkeys(
                [
                    # 先保留 State 中已有的执行顺序。
                    *state.get("tool_call_fingerprints", []),
                    # 再补充历史中可能缺失的指纹。
                    *(record.fingerprint for record in history),
                ]
            )
        )
        # 集合只用于常数时间成员判断，不用于最终状态序列化。
        known_fingerprints = set(ordered_fingerprints)
        # 重复计划通常意味着模型卡住，不能继续消耗工具或形成无限循环。
        if fingerprint in known_fingerprints:
            # 本次不执行也不追加重复结果。
            return {
                # 明确执行被拒绝。
                "agent_execution_succeeded": False,
                # 进入人工终止路径。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定码支持统计重复调用拦截率。
                "agent_failure_code": "duplicate_tool_call",
                # 保存停止原因。
                "agent_stop_reason": "duplicate_tool_call",
                # 自动循环未能取得新进展。
                "requires_human": True,
                # 用户参数已经存在，问题在规划循环本身。
                "needs_clarification": False,
                # 事件不泄漏具体重复参数。
                "events": ["graph:order_agent_duplicate_tool_call_blocked"],
            }

        # 到达这里后才创建绑定可信 user_id 的工具实例。
        order_tool = create_order_status_tool(user_id=user_id, repository=repository)
        # 当前调用将占用一个实际工具步数，无论外部仓库最终成功或抛错。
        next_tool_call_count = tool_call_count + 1
        try:
            # BaseTool.invoke 先通过 OrderLookupInput 校验模型建议参数。
            raw_tool_result = order_tool.invoke(tool_arguments)
        # 工具 Schema、仓库或 LangChain 执行异常统一转为脱敏失败观察。
        except Exception as error:
            # 日志只记录异常类名，不记录参数、仓库响应或异常正文。
            logger.warning(
                "订单工具执行失败并降级到人工: request_id=%s cause_type=%s",
                request_id,
                type(error).__name__,
            )
            # 构造不包含原始异常信息的失败执行记录。
            failure_record = ToolExecutionRecord(
                # 保存实际白名单工具名。
                tool_name=plan.tool_name,
                # 保存计划参数供请求内审计和重复检测。
                arguments=tool_arguments,
                # 保存本次稳定调用指纹。
                fingerprint=fingerprint,
                # 工具或参数校验没有成功完成。
                succeeded=False,
                # 有限错误码不暴露根因正文。
                error_code="tool_execution_error",
            )
            # 返回失败观察并立即终止自动循环。
            return {
                # 累积失败记录，后续 Trace 能看到尝试次数。
                "tool_execution_records": [*history, failure_record],
                # 已经尝试的指纹不能在同一请求内重试。
                "tool_call_fingerprints": [*ordered_fingerprints, fingerprint],
                # 实际执行尝试占用一步预算。
                "tool_call_count": next_tool_call_count,
                # 执行失败。
                "agent_execution_succeeded": False,
                # 下一跳进入人工。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定错误码供人工文案和指标使用。
                "agent_failure_code": "tool_execution_error",
                # 循环停止于工具异常。
                "agent_stop_reason": "tool_execution_error",
                # 请求需要人工完成。
                "requires_human": True,
                # 系统故障不是用户缺少参数。
                "needs_clarification": False,
                # 事件不包含异常正文。
                "events": ["graph:order_tool_execution_error_fallback_to_human"],
            }

        try:
            # 工具成功返回后仍通过领域 Schema 二次校验，不能直接信任任意字典。
            tool_result = OrderLookupResult.model_validate(raw_tool_result)
        # 输出缺字段、类型错误或被替换工具返回异常结构时安全停止。
        except Exception as error:
            # 只记录校验异常类名，不记录可能含业务数据的原始结果。
            logger.warning(
                "订单工具输出校验失败并降级到人工: request_id=%s cause_type=%s",
                request_id,
                type(error).__name__,
            )
            # 构造结构化输出失败记录。
            failure_record = ToolExecutionRecord(
                # 保存已执行工具名。
                tool_name=plan.tool_name,
                # 保存计划参数。
                arguments=tool_arguments,
                # 保存稳定指纹。
                fingerprint=fingerprint,
                # 结果没有通过领域验证。
                succeeded=False,
                # 与外部执行错误区分的有限类别。
                error_code="invalid_tool_result",
            )
            # 失败结果不能进入规划器或最终回答。
            return {
                # 只追加不含原始结果的失败记录。
                "tool_execution_records": [*history, failure_record],
                # 记录本次已尝试指纹。
                "tool_call_fingerprints": [*ordered_fingerprints, fingerprint],
                # 本次真实调用已经发生，因此计数加一。
                "tool_call_count": next_tool_call_count,
                # 执行结果不可接受。
                "agent_execution_succeeded": False,
                # 进入人工路径。
                "agent_next_action": AgentAction.HANDOFF.value,
                # 稳定结果校验故障码。
                "agent_failure_code": "invalid_tool_result",
                # 记录停止原因。
                "agent_stop_reason": "invalid_tool_result",
                # 自动处理失败。
                "requires_human": True,
                # 不要求用户重新提交同一参数。
                "needs_clarification": False,
                # 不暴露半截工具结果。
                "events": ["graph:order_tool_invalid_result_fallback_to_human"],
            }

        # 工具调用和输出 Schema 均成功，构造可供下一轮规划观察的记录。
        success_record = ToolExecutionRecord(
            # 使用 BaseTool 的真实稳定名称，而不是再次信任计划文本。
            tool_name=order_tool.name,
            # 参数已经通过 OrderLookupInput 校验；工具结果回显规范订单号。
            arguments={"order_id": tool_result.order_id},
            # 指纹与调用前检查使用同一个值。
            fingerprint=fingerprint,
            # 找不到订单仍是一次技术上成功的安全工具调用。
            succeeded=True,
            # mode=json 把枚举等值转换为后续可稳定序列化的基础类型。
            result=tool_result.model_dump(mode="json"),
        )
        # 查询订单号列表按首次实际执行顺序保序去重。
        queried_order_ids = list(
            # dict.fromkeys 保留原列表顺序。
            dict.fromkeys([*state.get("queried_order_ids", []), tool_result.order_id])
        )
        # 成功找到与未找到使用不同业务事件，但都会返回规划节点继续判断停止条件。
        outcome_event = (
            # 找到本人订单时保留已有成功事件，兼容前面步骤的指标与测试。
            "graph:order_lookup_succeeded"
            # 找不到或不属于当前用户时使用统一不可用事件。
            if tool_result.found
            else "graph:order_lookup_not_available"
        )
        # 写入工具观察并回到规划器，形成显式 LangGraph 环。
        return {
            # 追加经过领域校验的成功记录。
            "tool_execution_records": [*history, success_record],
            # 追加指纹，阻止后续重复调用。
            "tool_call_fingerprints": [*ordered_fingerprints, fingerprint],
            # 更新已实际执行步数。
            "tool_call_count": next_tool_call_count,
            # 保存所有已查询订单号供 API 和最终回答使用。
            "queried_order_ids": queried_order_ids,
            # 兼容原单订单 API 字段，当前值是最近一次查询订单号。
            "order_id": tool_result.order_id,
            # 公开实际工具名供教学 Trace 使用。
            "tool_name": order_tool.name,
            # 保存最近一次结构化结果，完整历史另存于执行记录。
            "tool_result": tool_result.model_dump(mode="json"),
            # 本次执行和领域校验都成功。
            "agent_execution_succeeded": True,
            # continue 由执行后条件边映射回规划节点。
            "agent_next_action": "continue",
            # 技术调用成功，无需人工。
            "requires_human": False,
            # 是否需要核对订单号在最终汇总时根据 found 判断。
            "needs_clarification": False,
            # 第一项标记工具节点，第二项保留具体业务结果事件。
            "events": ["graph:order_tool_executed", outcome_event],
        }

    # 返回已经绑定仓库与最大工具步数的同步执行节点。
    return execute_order_tool


def finalize_order_agent_response(state: ServiceState) -> StateUpdate:
    """把所有成功工具观察汇总成不依赖模型改写的最终订单回答。"""

    # 只读取通过 ToolExecutionRecord 运行时校验且 succeeded=True 的观察。
    successful_records = [record for record in _validated_history(state) if record.succeeded]
    # 理论上 finish 路由已保证存在观察；防御空历史避免返回虚假成功。
    if not successful_records:
        # 最终节点没有后续条件边，因此直接生成安全人工提示并标记失败。
        return {
            # 不声称完成任何订单查询。
            "answer": "自动工具执行未能取得有效结果，本次请求已建议转交人工客服。",
            # 调用方应创建人工任务。
            "requires_human": True,
            # 系统执行异常不是用户缺少订单号。
            "needs_clarification": False,
            # 稳定错误码帮助发现错误 finish 路由。
            "agent_failure_code": "finish_without_tool_result",
            # 保存停止原因。
            "agent_stop_reason": "finish_without_tool_result",
            # 事件记录最终安全拒绝。
            "events": ["graph:order_agent_finish_without_result_blocked"],
        }

    # validated_results 保存每条再次通过 OrderLookupResult 校验的观察。
    validated_results: list[OrderLookupResult] = []
    try:
        # 执行记录虽已校验外层结构，最终使用前仍校验具体订单领域字段。
        validated_results = [
            # 防止 State 序列化或外部 Checkpointer 恢复后结果结构被破坏。
            OrderLookupResult.model_validate(record.result)
            # 保持工具实际执行顺序。
            for record in successful_records
        ]
    # 任何历史结果异常都不能被字符串拼接进用户回答。
    except Exception as error:
        # 仅记录异常类名和请求标识，不记录具体结果。
        logger.warning(
            "订单 Agent 最终观察校验失败: request_id=%s cause_type=%s",
            state.get("request_id", "unknown"),
            type(error).__name__,
        )
        # 返回安全人工状态。
        return {
            # 固定文案不包含无效结果内容。
            "answer": "订单查询结果校验失败，本次请求已建议转交人工客服。",
            # 自动回答未完成。
            "requires_human": True,
            # 用户无需重复提供参数。
            "needs_clarification": False,
            # 稳定错误码用于监控。
            "agent_failure_code": "final_observation_invalid",
            # 保存停止原因。
            "agent_stop_reason": "final_observation_invalid",
            # 事件不包含异常正文。
            "events": ["graph:order_agent_final_observation_invalid"],
        }

    # answer_lines 为每个查询结果生成一行确定性用户说明。
    answer_lines: list[str] = []
    # 顺序遍历所有结果，支持一个请求查询多个订单。
    for result in validated_results:
        # 每行先使用工具领域模型提供的安全消息。
        answer_parts = [result.message]
        # 只有存在承运商时才追加该事实。
        if result.carrier:
            # 承运商来自归属校验后的仓库结果。
            answer_parts.append(f"承运商：{result.carrier}。")
        # 只有存在物流单号时才追加该事实。
        if result.tracking_number:
            # 物流单号绝不从模型文本生成。
            answer_parts.append(f"物流单号：{result.tracking_number}。")
        # 当前订单的各句使用空格连接。
        answer_lines.append(" ".join(answer_parts))

    # 只要有一条订单不可用，就提示调用方可能需要用户核对订单号。
    needs_clarification = any(not result.found for result in validated_results)
    # 最近一次结果用于兼容原有单订单字段。
    latest_result = validated_results[-1]
    # 返回正常结束的全部状态增量。
    return {
        # 多订单每条占一行，单订单输出与旧实现保持一致。
        "answer": "\n".join(answer_lines),
        # 最近一次订单号用于兼容 ChatResponse.order_id。
        "order_id": latest_result.order_id,
        # 工具名称固定为经过白名单执行的只读查询工具。
        "tool_name": "get_order_status",
        # 最近一次结果用于兼容旧调试字段。
        "tool_result": latest_result.model_dump(mode="json"),
        # 返回所有实际查询订单号，支持前端展示多工具执行。
        "queried_order_ids": [result.order_id for result in validated_results],
        # 正常到达汇总节点表示 Agent 循环已满足停止条件。
        "agent_stop_reason": "completed",
        # 不需要人工；未找到订单属于安全业务结果而不是系统故障。
        "requires_human": False,
        # 不可用订单需要用户核对，全部找到则无需补充。
        "needs_clarification": needs_clarification,
        # 事件明确记录循环正常完成。
        "events": ["graph:order_agent_completed"],
    }


def clarify_order_request(state: ServiceState) -> StateUpdate:
    """在规划器确认缺少订单号时生成稳定追问。"""

    # 保留当前已执行次数；正常缺参通常为零，但该节点不隐式重置 State。
    _ = state.get("tool_call_count", 0)
    # 返回与第二步兼容的确定性提示。
    return {
        # 明确告诉用户合法格式，下一轮可以直接补充。
        "answer": "请提供要查询的订单号，格式例如 SO100001。",
        # 当前仍可自动继续，不需要人工客服。
        "requires_human": False,
        # API 据此提示继续收集参数。
        "needs_clarification": True,
        # 循环停止等待新的用户输入。
        "agent_stop_reason": "needs_clarification",
        # 保留已有稳定事件，兼容历史指标。
        "events": ["graph:order_id_required"],
    }
