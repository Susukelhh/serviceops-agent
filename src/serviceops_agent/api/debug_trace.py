"""把 LangGraph StateSnapshot 转换成适合教学展示的脱敏回放。

真实 Checkpoint 是整张“工作单快照”，其中可能包含用户身份、幂等键、审批人身份、
Token jti 和工具去重指纹。本模块采用字段白名单，而不是把数据库原始行直接交给浏览器。
因此页面可以解释节点、状态、条件边、工具、RAG 和 interrupt，却不会成为敏感数据导出器。
"""

# Mapping/Sequence 支持递归处理字典与列表，但字符串会在更早的分支单独处理。
from collections.abc import Callable, Mapping, Sequence

# dataclass 把每个可公开 State 字段的中文说明与清洗函数固定在代码中。
from dataclasses import dataclass

# Enum 支持把 StrEnum 等有限枚举转换成普通 JSON 字符串。
from enum import Enum

# Any 只用于读取 LangGraph 元数据的动态字典；对外值仍必须收敛到 JsonValue。
from typing import Any, Literal

# StateSnapshot 是 LangGraph 官方状态历史对象，包含 values、next、config 和 tasks。
from langgraph.types import StateSnapshot

# BaseModel 识别 ToolCallPlan、Citation 等强类型状态；JsonValue 是最终安全输出边界。
from pydantic import BaseModel, JsonValue

# API Schema 保证本模块返回的数据结构能被 FastAPI 再次验证和生成 OpenAPI 文档。
from serviceops_agent.api.schemas import (
    DebugCheckpoint,
    DebugInterruptSummary,
    DebugNodeReference,
    DebugStateChange,
    DebugStateField,
    ThreadDebugResponse,
)

# 教学接口最多返回最近 200 个快照，避免异常长线程一次占用过多内存和网络带宽。
MAX_DEBUG_CHECKPOINTS = 200

# 任意嵌套对象只要出现这些键都必须隐藏，形成白名单之外的第二层纵深防御。
SENSITIVE_NESTED_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "comment",
        "fingerprint",
        "idempotency_key",
        "outbox_event_id",
        "password",
        "reviewer_id",
        "secret",
        "thread_id",
        "token_jti",
        "user_id",
    }
)


def _safe_json(value: object, *, depth: int = 0) -> JsonValue:
    """把已进入字段白名单的对象递归转换为有限、脱敏的 JSON 值。"""

    # 最多展开六层，防止意外自引用或深层第三方对象造成递归和超大响应。
    if depth > 6:
        return "<内容层级过深，已省略>"
    # None、字符串、布尔和数值本身就是合法 JSON 标量。
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    # StrEnum/Enum 使用稳定 value；未知枚举值仍转成字符串而不是对象 repr。
    if isinstance(value, Enum):
        return str(value.value)
    # Pydantic 对象先使用 JSON 模式导出，再继续应用嵌套敏感键过滤。
    if isinstance(value, BaseModel):
        return _safe_json(value.model_dump(mode="json"), depth=depth + 1)
    # Mapping 只保留字符串化键，并对每一层重新执行敏感键检查。
    if isinstance(value, Mapping):
        safe_mapping: dict[str, JsonValue] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key.lower() in SENSITIVE_NESTED_KEYS:
                continue
            safe_mapping[key] = _safe_json(raw_value, depth=depth + 1)
        return safe_mapping
    # list/tuple 等顺序容器按原顺序转换；字符串已经在上面被当作标量处理。
    if isinstance(value, Sequence):
        return [_safe_json(item, depth=depth + 1) for item in value]
    # 未识别对象不调用 repr，避免异常对象把连接串或内部地址带入响应。
    return f"<{type(value).__name__} 已省略>"


def _safe_retrieval_hits(value: object) -> JsonValue:
    """公开 RAG 候选的标题、分数和短证据，不暴露完整向量或内部 Payload。"""

    # 非列表值说明状态异常，返回稳定空列表而不是尝试猜测结构。
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    safe_hits: list[JsonValue] = []
    for raw_hit in value:
        # RetrievalHit 是 Pydantic 模型；其他测试替身也允许使用安全字典。
        hit = raw_hit.model_dump(mode="json") if isinstance(raw_hit, BaseModel) else raw_hit
        if not isinstance(hit, Mapping):
            continue
        raw_chunk = hit.get("chunk")
        if not isinstance(raw_chunk, Mapping):
            continue
        # 正文只截取前 240 字帮助理解命中原因；完整知识仍留在后端受治理文档中。
        content = str(raw_chunk.get("content", ""))[:240]
        safe_hits.append(
            {
                "document_id": str(raw_chunk.get("document_id", "")),
                "chunk_id": str(raw_chunk.get("chunk_id", "")),
                "title": str(raw_chunk.get("title", "")),
                "score": _safe_json(hit.get("score")),
                "content_preview": content,
            }
        )
    return safe_hits


def _safe_tool_records(value: object) -> JsonValue:
    """展示工具名、受控参数与校验结果，同时删除 SHA-256 去重指纹。"""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    records: list[JsonValue] = []
    for raw_record in value:
        record = (
            raw_record.model_dump(mode="json") if isinstance(raw_record, BaseModel) else raw_record
        )
        if not isinstance(record, Mapping):
            continue
        records.append(
            {
                "tool_name": _safe_json(record.get("tool_name")),
                "arguments": _safe_json(record.get("arguments", {})),
                "succeeded": _safe_json(record.get("succeeded")),
                "result": _safe_json(record.get("result", {})),
                "error_code": _safe_json(record.get("error_code")),
            }
        )
    return records


def _safe_return_proposal(value: object) -> JsonValue:
    """保留审批人需要理解的草案字段，但绝不公开幂等键。"""

    proposal = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(proposal, Mapping):
        return None
    return {
        "action": _safe_json(proposal.get("action")),
        "order_id": _safe_json(proposal.get("order_id")),
        "reason": _safe_json(proposal.get("reason")),
        "risk_level": _safe_json(proposal.get("risk_level")),
    }


def _safe_approval_decision(value: object) -> JsonValue:
    """只公开批准/拒绝结论，不公开审批人、备注、线程上下文和 Token jti。"""

    decision = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    if not isinstance(decision, Mapping):
        return None
    return {"approved": _safe_json(decision.get("approved"))}


# sanitizer 允许少数字段使用专用清洗逻辑；其他字段使用统一 JSON 转换器。
type DebugSanitizer = Callable[[object], JsonValue]
# category 与 API Schema 使用同一组有限值，避免任意分类进入前端 CSS 和筛选逻辑。
type DebugCategory = Literal[
    "input",
    "routing",
    "retrieval",
    "tool",
    "approval",
    "output",
    "safety",
    "trace",
]
# change_type 只允许表达状态字段的三种基本变化。
type DebugChangeType = Literal["added", "updated", "removed"]
# workflow status 只表达读取时线程是否结束、暂停或仍有待执行节点。
type DebugWorkflowStatus = Literal["completed", "waiting_approval", "in_progress"]


@dataclass(frozen=True, slots=True)
class DebugFieldPolicy:
    """一个允许进入教学接口的 ServiceState 字段策略。"""

    # label 是页面展示的中文字段名。
    label: str
    # category 用于前端按输入、路由、检索、工具、审批等主题筛选。
    category: DebugCategory
    # description 解释写入方和消费方，而不是重复字段名称。
    description: str
    # sanitizer 决定如何安全序列化该字段。
    sanitizer: DebugSanitizer = _safe_json


# 只有出现在这个映射中的字段才可能离开后端。user_id、idempotency_key、
# token_jti、tool_call_fingerprints 和 outbox_event_id 故意不在白名单中。
DEBUG_FIELD_POLICIES: dict[str, DebugFieldPolicy] = {
    "request_id": DebugFieldPolicy("请求编号", "trace", "API 创建，用于关联本次执行。"),
    "user_message": DebugFieldPolicy("用户原话", "input", "API 接收的原始问题，供后续节点读取。"),
    "normalized_message": DebugFieldPolicy(
        "规范化问题", "input", "normalize_request 清理空白后写入。"
    ),
    "intent": DebugFieldPolicy("业务意图", "routing", "classify_intent 的结构化分类结果。"),
    "route_reason": DebugFieldPolicy("路由依据", "routing", "分类器给出的短依据，不是隐藏思维链。"),
    "intent_confidence": DebugFieldPolicy("分类置信度", "routing", "分类器给出的有限数值。"),
    "requires_human": DebugFieldPolicy("需要人工", "safety", "节点判断自动化是否应停止。"),
    "needs_clarification": DebugFieldPolicy(
        "需要补充信息", "safety", "缺少订单号等必要参数时为真。"
    ),
    "llm_failure_code": DebugFieldPolicy("模型故障码", "safety", "模型失败的脱敏稳定分类。"),
    "rag_failure_code": DebugFieldPolicy("检索故障码", "safety", "RAG 基础设施失败的脱敏分类。"),
    "rag_no_evidence": DebugFieldPolicy(
        "没有合格证据", "retrieval", "最高候选未通过证据阈值时为真。"
    ),
    "has_sufficient_evidence": DebugFieldPolicy(
        "证据充足", "retrieval", "检索节点是否允许进入回答节点。"
    ),
    "retrieval_score": DebugFieldPolicy("最高检索分数", "retrieval", "当前查询的最高余弦相似度。"),
    "retrieval_hits": DebugFieldPolicy(
        "检索候选", "retrieval", "通过治理后的候选、分数与证据预览。", _safe_retrieval_hits
    ),
    "citations": DebugFieldPolicy("引用", "retrieval", "回答节点最终采用的公开知识来源。"),
    "faq_answer_grounded": DebugFieldPolicy(
        "回答有据", "retrieval", "答案与引用白名单校验是否通过。"
    ),
    "answer": DebugFieldPolicy("最终回答", "output", "终点节点生成的用户可见结果。"),
    "order_id": DebugFieldPolicy("订单号", "tool", "规划器或工具路径使用的规范订单号。"),
    "tool_name": DebugFieldPolicy("实际工具", "tool", "执行器真正调用的白名单工具名称。"),
    "tool_result": DebugFieldPolicy("工具结果", "tool", "经过领域 Schema 校验的结构化观察。"),
    "planned_tool_call": DebugFieldPolicy(
        "工具计划", "tool", "模型或基线规划器提交、尚待执行器复查的计划。"
    ),
    "tool_call_count": DebugFieldPolicy("工具次数", "tool", "本线程已经真实尝试的工具次数。"),
    "tool_execution_records": DebugFieldPolicy(
        "工具观察历史", "tool", "去除指纹后的参数、成功标记和结果。", _safe_tool_records
    ),
    "queried_order_ids": DebugFieldPolicy(
        "已查询订单", "tool", "工具按实际执行顺序查询过的订单号。"
    ),
    "agent_next_action": DebugFieldPolicy("下一动作", "routing", "条件边只接受的有限 Agent 动作。"),
    "agent_execution_succeeded": DebugFieldPolicy(
        "工具执行成功", "safety", "调用和结果校验是否同时通过。"
    ),
    "agent_failure_code": DebugFieldPolicy("Agent 故障码", "safety", "循环失败的脱敏有限分类。"),
    "agent_stop_reason": DebugFieldPolicy("停止原因", "routing", "循环完成、澄清或安全退出原因。"),
    "return_request_proposal": DebugFieldPolicy(
        "退货草案", "approval", "审批前的动作、订单、原因和风险级别。", _safe_return_proposal
    ),
    "approval_decision": DebugFieldPolicy(
        "审批结论", "approval", "仅显示批准布尔值，身份和备注已隐藏。", _safe_approval_decision
    ),
    "approval_required": DebugFieldPolicy(
        "等待审批", "approval", "写工具前是否正处于 interrupt 暂停。"
    ),
    "return_workflow_status": DebugFieldPolicy(
        "退货流程阶段", "approval", "草案、待批、批准、拒绝或完成阶段。"
    ),
    "return_request_id": DebugFieldPolicy(
        "退货申请号", "output", "批准并写入成功后生成的业务编号。"
    ),
    "events": DebugFieldPolicy("公开事件", "trace", "各节点通过 Reducer 追加的有限执行事件。"),
}


# 节点说明与 graph/builder.py 的稳定注册名一一对应，未知节点仍会安全回退。
NODE_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "__start__": ("写入初始状态", "把 API 输入放入 LangGraph State。"),
    "normalize_request": ("规范化输入", "清理用户文本但保留原话。"),
    "classify_intent": ("识别业务意图", "输出有限意图、置信度和短路由依据。"),
    "retrieve_faq": ("检索企业知识", "检查业务范围，召回固定候选，按配置重排并执行证据阈值检查。"),
    "answer_faq": ("生成有据回答", "只根据合格证据组织答案并校验引用。"),
    "initialize_order_agent": ("初始化工具循环", "清空本轮计数、计划和观察历史。"),
    "plan_order_action": ("规划下一动作", "只输出调用工具、结束、澄清或转人工之一。"),
    "execute_order_tool": ("执行订单工具", "复查白名单、身份、参数、步数和结果 Schema。"),
    "finalize_order_response": ("汇总订单结果", "根据可信工具观察生成确定性回答。"),
    "clarify_order_request": ("追问必要参数", "缺订单号时停止工具调用并向用户补问。"),
    "prepare_return_request": ("准备退货草案", "只读检查订单归属和资格，不写业务数据。"),
    "request_return_approval": ("等待人工审批", "调用 interrupt 保存快照并暂停写操作。"),
    "execute_return_request": ("执行退货写工具", "只在明确批准后幂等创建退货申请。"),
    "finalize_return_rejection": ("结束已拒绝流程", "生成拒绝结果且不调用写工具。"),
    "handoff_to_human": ("转交人工", "在证据不足或安全异常时停止自动化。"),
}


def _node_reference(node_name: str) -> DebugNodeReference:
    """把稳定节点名转换为可以直接在页面理解和搜索的引用。"""

    label, description = NODE_DESCRIPTIONS.get(
        node_name,
        (node_name, "框架或未来版本注册的节点，当前没有额外教学说明。"),
    )
    return DebugNodeReference(name=node_name, label=label, description=description)


def _safe_state(values: Mapping[str, Any]) -> dict[str, JsonValue]:
    """按字段白名单构造一个快照的安全状态字典。"""

    safe_values: dict[str, JsonValue] = {}
    # 使用策略声明顺序，保证页面和测试每次展示顺序一致。
    for field_name, policy in DEBUG_FIELD_POLICIES.items():
        if field_name in values:
            safe_values[field_name] = policy.sanitizer(values[field_name])
    return safe_values


def _state_fields(safe_values: Mapping[str, JsonValue]) -> list[DebugStateField]:
    """把安全状态字典附加中文字段解释。"""

    fields: list[DebugStateField] = []
    for field_name, value in safe_values.items():
        policy = DEBUG_FIELD_POLICIES[field_name]
        fields.append(
            DebugStateField(
                name=field_name,
                label=policy.label,
                category=policy.category,
                description=policy.description,
                value=value,
            )
        )
    return fields


def _state_changes(
    previous: Mapping[str, JsonValue],
    current: Mapping[str, JsonValue],
) -> list[DebugStateChange]:
    """比较两个已经脱敏的状态，避免差异计算重新接触敏感原值。"""

    changes: list[DebugStateChange] = []
    for field_name, policy in DEBUG_FIELD_POLICIES.items():
        was_present = field_name in previous
        is_present = field_name in current
        if not was_present and not is_present:
            continue
        before = previous.get(field_name)
        after = current.get(field_name)
        if was_present and is_present and before == after:
            continue
        change_type: DebugChangeType = (
            "updated" if was_present and is_present else ("added" if is_present else "removed")
        )
        changes.append(
            DebugStateChange(
                name=field_name,
                label=policy.label,
                category=policy.category,
                change_type=change_type,
                before=before,
                after=after,
            )
        )
    return changes


def _checkpoint_config_value(config: object, key: str) -> str | None:
    """从 RunnableConfig 中安全读取 checkpoint_id 等字符串值。"""

    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    value = configurable.get(key)
    return str(value) if value is not None else None


def _interrupt_summary(snapshot: StateSnapshot) -> DebugInterruptSummary | None:
    """从 StateSnapshot 或待执行任务中提取第一条安全 interrupt。"""

    raw_interrupts: list[object] = list(snapshot.interrupts)
    # 当前 LangGraph 版本主要把 interrupt 保存在 PregelTask.interrupts 中。
    for task in snapshot.tasks:
        raw_interrupts.extend(task.interrupts)
    if not raw_interrupts:
        return None
    raw_value = getattr(raw_interrupts[0], "value", None)
    if not isinstance(raw_value, Mapping):
        return DebugInterruptSummary(kind="unknown_interrupt")
    return DebugInterruptSummary(
        kind=str(raw_value.get("kind", "unknown_interrupt")),
        action=(str(raw_value["action"]) if raw_value.get("action") is not None else None),
        order_id=(str(raw_value["order_id"]) if raw_value.get("order_id") is not None else None),
        risk_level=(
            str(raw_value["risk_level"]) if raw_value.get("risk_level") is not None else None
        ),
        message=(str(raw_value["message"]) if raw_value.get("message") is not None else None),
    )


def _decision_summary(
    *,
    safe_values: Mapping[str, JsonValue],
    next_nodes: tuple[str, ...],
    interrupt: DebugInterruptSummary | None,
) -> str:
    """根据公开结构化字段解释条件边结果，不生成或猜测隐藏思维过程。"""

    if interrupt is not None:
        return "人工审批节点已调用 interrupt：Checkpoint 已保存，写工具尚未执行。"
    if not next_nodes:
        return "没有待执行节点：本轮状态图已经到达终点。"
    next_node = next_nodes[0]
    intent = safe_values.get("intent")
    action = safe_values.get("agent_next_action")
    if next_node == "retrieve_faq":
        return f"结构化意图为 {intent}，条件边选择进入企业知识检索。"
    if next_node == "initialize_order_agent":
        return f"结构化意图为 {intent}，条件边选择启动受控订单工具循环。"
    if next_node == "prepare_return_request":
        return "结构化意图为 return_request，先做只读资格检查和草案准备。"
    if next_node == "answer_faq":
        return "has_sufficient_evidence=true，证据门允许进入有据回答节点。"
    if next_node == "execute_order_tool":
        return f"规划器的有限动作是 {action}，条件边进入唯一工具执行边界。"
    if next_node == "plan_order_action":
        return "工具观察已通过校验，回到规划器决定结束还是继续调用。"
    if next_node == "finalize_order_response":
        return f"规划器的有限动作是 {action}，现有工具观察足以生成最终回答。"
    if next_node == "clarify_order_request":
        return "缺少工具必需参数，条件边选择追问而不是猜测或盲调工具。"
    if next_node == "request_return_approval":
        return "退货资格和草案校验通过，下一步必须先进入人工审批中断。"
    if next_node == "execute_return_request":
        return "审批结论为 approved=true，条件边才允许进入退货写工具。"
    if next_node == "finalize_return_rejection":
        return "审批结论为 approved=false，流程进入零写入的拒绝终点。"
    if next_node == "handoff_to_human":
        return "自动化安全条件未满足，条件边选择停止并转交人工。"
    if next_node == "normalize_request":
        return "初始输入已经写入 State，下一步先规范化文本。"
    if next_node == "classify_intent":
        return "文本规范化完成，下一步输出有限的结构化业务意图。"
    if next_node == "__start__":
        return "LangGraph 已创建输入快照，下一步把 API 输入写入共享 State。"
    return f"图的已验证边选择下一节点：{next_node}。"


def build_thread_debug_response(
    *,
    thread_id: str,
    newest_first_snapshots: Sequence[StateSnapshot],
    truncated: bool,
) -> ThreadDebugResponse:
    """把 LangGraph 最新优先历史转换为最早优先的教学回放响应。"""

    # LangGraph 官方 API 返回最新快照在前；页面播放需要反转为时间正序。
    chronological_snapshots = list(reversed(newest_first_snapshots))
    previous_safe_values: dict[str, JsonValue] = {}
    previous_next_nodes: tuple[str, ...] = ()
    checkpoints: list[DebugCheckpoint] = []

    for index, snapshot in enumerate(chronological_snapshots):
        current_safe_values = _safe_state(snapshot.values)
        interrupt = _interrupt_summary(snapshot)
        metadata = snapshot.metadata or {}
        raw_step = metadata.get("step", -1)
        step = raw_step if isinstance(raw_step, int) else -1
        source = str(metadata.get("source", "unknown"))
        checkpoint_id = _checkpoint_config_value(snapshot.config, "checkpoint_id")
        # 一个真实 StateSnapshot 必须有 checkpoint_id；异常缺失时使用稳定占位而不泄漏 repr。
        stable_checkpoint_id = checkpoint_id or f"missing-checkpoint-{index + 1}"
        parent_id = _checkpoint_config_value(snapshot.parent_config, "checkpoint_id")
        # 从上一快照的 next 可以准确解释“刚刚执行了哪个顺序节点”。
        executed_nodes = [_node_reference(name) for name in previous_next_nodes]
        next_nodes = [_node_reference(name) for name in snapshot.next]
        checkpoints.append(
            DebugCheckpoint(
                position=index + 1,
                checkpoint_id=stable_checkpoint_id,
                parent_checkpoint_id=parent_id,
                step=step,
                source=source,
                created_at=snapshot.created_at,
                executed_nodes=executed_nodes,
                next_nodes=next_nodes,
                decision_summary=_decision_summary(
                    safe_values=current_safe_values,
                    next_nodes=snapshot.next,
                    interrupt=interrupt,
                ),
                state_changes=_state_changes(previous_safe_values, current_safe_values),
                state_fields=_state_fields(current_safe_values),
                has_interrupt=interrupt is not None,
                interrupt=interrupt,
                # 只返回布尔值；PregelTask.error 原文可能包含内部连接或第三方响应。
                has_error=any(task.error is not None for task in snapshot.tasks),
            )
        )
        previous_safe_values = current_safe_values
        previous_next_nodes = snapshot.next

    latest = chronological_snapshots[-1]
    latest_interrupt = _interrupt_summary(latest)
    if latest_interrupt is not None:
        status: DebugWorkflowStatus = "waiting_approval"
    elif latest.next:
        status = "in_progress"
    else:
        status = "completed"
    return ThreadDebugResponse(
        thread_id=thread_id,
        status=status,
        checkpoint_count=len(checkpoints),
        truncated=truncated,
        hidden_reasoning_exposed=False,
        disclosure=(
            "展示的是节点、结构化模型结论、状态差异、Checkpoint、工具/RAG 与审批边界；"
            "隐藏推理原文、用户身份、Token、幂等键、审批人和内部指纹不会返回。"
        ),
        checkpoints=checkpoints,
    )
