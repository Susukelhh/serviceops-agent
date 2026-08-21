"""定义节点之间共享的 LangGraph 状态。"""

# operator.add 在这里作为 Reducer：把多个节点返回的事件列表按执行顺序相加。
from operator import add

# Annotated 为字段附加 Reducer 元数据；TypedDict 描述整张图共享状态的字段和类型。
from typing import Annotated, TypedDict

# ToolCallPlan/ToolExecutionRecord 为显式模型—工具—观察循环提供强类型状态。
from serviceops_agent.domain.agent import ToolCallPlan, ToolExecutionRecord

# Citation/RetrievalHit 为 FAQ RAG 路径提供带类型的证据和引用。
from serviceops_agent.domain.knowledge import Citation, RetrievalHit


class ServiceState(TypedDict, total=False):
    """一次工单处理任务的共享状态。

    `total=False` 表示图执行到不同阶段时，部分字段可以暂时不存在。例如 `intent` 只有在
    分类节点运行后才会出现，`answer` 只有响应节点运行后才会出现。

    普通字段默认采用“后写覆盖前写”的合并方式；`events` 使用 `operator.add` 作为 Reducer，
    所以每个节点返回的新事件会被追加。这个差异是理解 LangGraph State 的重点。
    """

    # API 层为每次请求生成的唯一标识；贯穿整张图，后续用于日志、追踪和故障定位。
    request_id: str
    # 发起请求的用户标识；后续查询订单、检查数据权限和加载用户记忆时会使用它。
    user_id: str
    # 用户最初提交的原始文本；保留原文便于审计，任何清洗都不覆盖该字段。
    user_message: str
    # 预处理节点生成的规范化文本；分类、检索和模型节点优先读取这个字段。
    normalized_message: str

    # 分类结果保存 Intent.value 字符串；条件路由仍只接受枚举定义的有限值。
    intent: str
    # 分类节点给出的路由原因；用于调试、审计和后续分析误分类样本。
    route_reason: str
    # 分类置信度；模型通道由 LLM 返回，关键词基线使用确定性的约定值。
    intent_confidence: float
    # 是否必须人工介入；API 响应和后续人工审批/转接模块都会读取该布尔值。
    requires_human: bool
    # 是否需要用户补充信息；例如订单问题缺少订单号时为 True，但不等同于转人工。
    needs_clarification: bool
    # 模型故障的内部稳定分类；正常请求不存在该字段，也不会直接暴露服务商错误正文。
    llm_failure_code: str
    # RAG 基础设施故障的内部稳定分类；正常检索不存在该字段。
    rag_failure_code: str
    # 没有达到证据阈值时为 True，用于人工节点生成准确的知识覆盖不足文案。
    rag_no_evidence: bool
    # FAQ 检索是否至少返回一条达到阈值的已发布公共证据。
    has_sufficient_evidence: bool
    # 当前查询最高命中的余弦相似度，供阈值评测和问题排查。
    retrieval_score: float
    # FAQ 回答实际允许使用的强类型证据命中，默认最多两条。
    retrieval_hits: list[RetrievalHit]
    # 可以安全返回给 API 的来源引用，不包含完整知识正文和高维向量。
    citations: list[Citation]
    # FAQ 生成答案是否通过“有证据、可回答、引用白名单”三项确定性校验。
    faq_answer_grounded: bool
    # 最终返回给用户的文本；只有 FAQ、订单或人工响应节点执行后才会出现。
    answer: str

    # 从用户文本中提取并规范化的订单号；订单查询节点和审计日志会读取它。
    order_id: str
    # 当前实际执行的工具名称；用于对外调试、内部 Trace 和工具调用成功率评测。
    tool_name: str
    # 工具返回的结构化字典；响应节点读取它生成安全、稳定的用户文本。
    tool_result: dict[str, object]
    # 规划器本轮建议、但尚未被工具执行器信任的一步结构化计划。
    planned_tool_call: ToolCallPlan | None
    # 当前请求已经实际尝试的工具次数，用于最大步数控制。
    tool_call_count: int
    # 工具名与参数的稳定摘要列表，用于同一请求内重复调用检测。
    tool_call_fingerprints: list[str]
    # 每次成功或失败工具尝试的强类型观察历史，下一轮规划器只能读取该列表。
    tool_execution_records: list[ToolExecutionRecord]
    # 多订单请求已经执行过查询的规范订单号列表。
    queried_order_ids: list[str]
    # 规划/执行节点写入的有限下一步动作，条件边只接受白名单字符串。
    agent_next_action: str
    # 最近一次工具执行是否通过调用和领域输出校验。
    agent_execution_succeeded: bool
    # Agent 循环失败的有限内部码；不会直接暴露第三方异常正文。
    agent_failure_code: str
    # 循环最终停止原因，例如 completed、needs_clarification 或 duplicate_tool_call。
    agent_stop_reason: str

    # API 提供或系统生成的幂等键；写工具重复恢复时必须保持不变。
    idempotency_key: str
    # 可持久化退货草案使用 JSON 字典保存；每个读取节点都会重新用 Pydantic 校验。
    return_request_proposal: dict[str, object]
    # 审批决定也以 JSON 字典进入 Checkpoint，读取边界重新校验为 ApprovalDecision。
    approval_decision: dict[str, object]
    # 当前是否正在等待外部审批输入。
    approval_required: bool
    # 退货流程阶段保存 StrEnum.value 字符串，避免 Checkpointer 反序列化自定义类。
    return_workflow_status: str
    # 写工具成功创建或幂等返回的退货申请编号。
    return_request_id: str
    # 生产 API 写事务生成的稳定 Outbox 事件编号；直接图教学调用可以没有。
    outbox_event_id: str | None

    # 执行事件列表；每个节点只追加自己的事件，`add` Reducer 确保旧事件不会被覆盖。
    events: Annotated[list[str], add]
