"""HTTP 接口使用的数据模型。

Pydantic 模型是 API 边界的第一层保护：不合法数据在进入 LangGraph 前就会被拒绝。
后续工具调用的参数也会采用同样的结构化校验思路。
"""

# Literal 用于把健康状态和调试状态限制为固定字符串，防止接口输出任意值。
from typing import Literal

# BaseModel 提供解析和序列化；JsonValue 约束调试值只能是安全 JSON；
# ConfigDict 禁止额外身份字段；StrictBool 防止字符串真值。
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool

# ApprovalAuditEvent 是只对 audit:read 主体公开的最小化审批证据记录。
from serviceops_agent.domain.audit import ApprovalAuditEvent

# Intent 是有限业务枚举，ChatResponse 会把它安全地序列化为 JSON 字符串。
from serviceops_agent.domain.enums import Intent

# Citation 是 RAG 路径允许对外暴露的脱敏知识来源，不包含完整向量或内部 payload。
from serviceops_agent.domain.knowledge import Citation

# 审批负载和退货流程枚举让 HTTP 边界与 LangGraph 内部状态保持强类型契约。
from serviceops_agent.domain.returns import ApprovalRequestPayload, ReturnWorkflowStatus


class HealthResponse(BaseModel):
    """健康检查响应。"""

    # 当前只有存活状态 `ok`；依赖服务健康度将在后续 readiness 接口中单独表达。
    status: Literal["ok"]
    # instance_id 是低敏稳定实例名，用于观察负载均衡是否把请求分给不同 API。
    instance_id: str
    # 返回 development/test/production，帮助确认请求实际到达了哪个运行环境。
    environment: str
    # 返回 memory/sqlite/postgres，便于确认服务实际使用哪一种持久化模式。
    persistence_backend: Literal["memory", "sqlite", "postgres"]


class DependencyCheck(BaseModel):
    """readiness 中一个内部依赖的有限健康结果。"""

    # status 只公开 ready/not_ready，不返回数据库异常消息和磁盘路径。
    status: Literal["ready", "not_ready"]


class ReadinessResponse(BaseModel):
    """应用是否可以安全接收 Agent 流量的依赖检查响应。"""

    # status 只有所有必需依赖都可读时才为 ready。
    status: Literal["ready", "not_ready"]
    # instance_id 帮助负载均衡器和运维人员区分是哪只 API 返回了本次结果。
    instance_id: str
    # checks 使用固定组件名映射有限结果，不泄漏连接串和异常正文。
    checks: dict[str, DependencyCheck]
    # persistence_backend 帮助确认实际检查的是内存、SQLite 还是 PostgreSQL 资源。
    persistence_backend: Literal["memory", "sqlite", "postgres"]
    # telemetry_exporter 展示 none/console/otlp_http 配置，不包含 Collector 端点或 Header。
    telemetry_exporter: Literal["disabled", "none", "console", "otlp_http"]


class ChatRequest(BaseModel):
    """用户提交的一条售后请求。"""

    # 禁止 user_id 等额外字段混入请求；身份只能来自 Bearer Token 的 sub Claim。
    model_config = ConfigDict(extra="forbid")

    # message 是用户原始问题；4000 字符上限防止无边界输入占用模型上下文和服务资源。
    message: str = Field(min_length=1, max_length=4000, description="用户的自然语言问题")
    # idempotency_key 由调用方在可重试写请求中稳定复用；只读请求可以省略。
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="可选的客户端幂等键；同一业务写请求重试时必须保持不变",
    )


class ApprovalDecisionRequest(BaseModel):
    """审批人恢复退货 interrupt 时提交的 HTTP 请求体。"""

    # 禁止 reviewer_id/user_id 等额外字段；审批身份只能来自具有 Scope 的 JWT。
    model_config = ConfigDict(extra="forbid")

    # 只有严格布尔 True 才代表批准；字符串等模糊真值会被 Pydantic 拒绝或转换校验。
    approved: StrictBool = Field(description="是否批准执行退货申请写操作")
    # comment 是可选短备注；它只进入内部审批状态，不返回普通用户。
    comment: str = Field(default="", max_length=500, description="审批备注")


class ApprovalAuditTrailResponse(BaseModel):
    """审计员读取某一 LangGraph 线程时获得的证据链。"""

    # thread_id 是被查询的原始工作流线程标识。
    thread_id: str
    # chain_valid 表示服务端已重新计算全部位置、前驱引用和事件哈希。
    chain_valid: bool
    # events 按 chain_position 升序返回，不包含原因、备注、幂等键或完整 Token。
    events: list[ApprovalAuditEvent]


class DebugNodeReference(BaseModel):
    """教学调试接口中的一个 LangGraph 节点引用。"""

    # name 是 builder.py 注册节点时使用的稳定英文标识，可直接在项目中搜索。
    name: str
    # label 是面向初学者的中文节点名称。
    label: str
    # description 用一句话解释该节点读取什么、写入什么或执行什么副作用。
    description: str


class DebugStateField(BaseModel):
    """一个 Checkpoint 中允许开发者查看的脱敏状态字段。"""

    # name 对应 ServiceState 的真实字段名，便于用户对照 state.py。
    name: str
    # label 是更容易理解的中文字段名。
    label: str
    # category 用有限业务分类帮助前端筛选模型、检索、工具和审批字段。
    category: Literal[
        "input",
        "routing",
        "retrieval",
        "tool",
        "approval",
        "output",
        "safety",
        "trace",
    ]
    # description 解释字段由谁写入、后续由谁读取。
    description: str
    # value 只允许 JSON 基础类型；任意 Python 对象必须先经过后端安全转换。
    value: JsonValue


class DebugStateChange(BaseModel):
    """相邻两个 Checkpoint 之间一个公开字段的变化。"""

    # name 是发生变化的 ServiceState 字段名。
    name: str
    # label 是字段的中文名称。
    label: str
    # category 与 DebugStateField 使用同一组有限分类。
    category: Literal[
        "input",
        "routing",
        "retrieval",
        "tool",
        "approval",
        "output",
        "safety",
        "trace",
    ]
    # change_type 区分字段首次出现、值被更新和字段被移除。
    change_type: Literal["added", "updated", "removed"]
    # before 是上一个 Checkpoint 的脱敏值；字段首次出现时为 None。
    before: JsonValue = None
    # after 是当前 Checkpoint 的脱敏值；字段被移除时为 None。
    after: JsonValue = None


class DebugInterruptSummary(BaseModel):
    """interrupt 的最小教学摘要，不复制框架内部任务对象。"""

    # kind 是中断业务类型，例如 return_request_approval。
    kind: str
    # action 是等待人工批准的有限动作。
    action: str | None = None
    # order_id 是写操作目标；非订单中断时可以为空。
    order_id: str | None = None
    # risk_level 明确本次暂停是否位于写操作边界。
    risk_level: str | None = None
    # message 是节点主动提供的固定审批说明，不是模型隐藏思维过程。
    message: str | None = None


class DebugCheckpoint(BaseModel):
    """一个可以在教学页面单步回放的 LangGraph StateSnapshot 摘要。"""

    # position 是前端显示的从 1 开始的时间顺序位置。
    position: int = Field(ge=1)
    # checkpoint_id 是 LangGraph 为当前快照生成的唯一标识。
    checkpoint_id: str
    # parent_checkpoint_id 指向上一个快照；第一个快照没有父节点。
    parent_checkpoint_id: str | None = None
    # step 是 LangGraph metadata 中的 super-step 计数，输入快照通常从 -1 开始。
    step: int
    # source 说明快照来自输入、图循环或显式状态更新。
    source: str
    # created_at 是 Checkpointer 记录的 ISO 8601 创建时间。
    created_at: str | None = None
    # executed_nodes 表示从上一个快照到当前快照实际完成了哪些节点。
    executed_nodes: list[DebugNodeReference]
    # next_nodes 表示恢复或继续执行时将进入哪些节点。
    next_nodes: list[DebugNodeReference]
    # decision_summary 只根据结构化状态和条件边结果生成，不包含模型隐藏推理原文。
    decision_summary: str
    # state_changes 是相邻安全状态的字段级差异。
    state_changes: list[DebugStateChange]
    # state_fields 是当前时刻允许查看的完整脱敏状态。
    state_fields: list[DebugStateField]
    # has_interrupt 表示该快照正停在人工中断边界。
    has_interrupt: bool
    # interrupt 是经过字段白名单处理的最小中断信息。
    interrupt: DebugInterruptSummary | None = None
    # has_error 只公开节点是否失败，不返回可能包含连接信息的异常正文。
    has_error: bool = False


class ThreadDebugResponse(BaseModel):
    """开发者读取一个 LangGraph 线程时获得的完整教学回放。"""

    # thread_id 是本次查询的 LangGraph 线程主键。
    thread_id: str
    # status 区分已完成、等待人工和仍有待执行节点三种状态。
    status: Literal["completed", "waiting_approval", "in_progress"]
    # checkpoint_count 是本次响应真正返回的快照数量。
    checkpoint_count: int = Field(ge=1)
    # truncated=True 表示线程超过教学接口的最大安全返回数量。
    truncated: bool
    # hidden_reasoning_exposed 永远为 False，明确本接口不是思维链泄漏开关。
    hidden_reasoning_exposed: Literal[False] = False
    # disclosure 用白话说明页面展示范围与敏感字段边界。
    disclosure: str
    # checkpoints 按最早到最晚排列，适合前端逐步播放。
    checkpoints: list[DebugCheckpoint]


class ChatResponse(BaseModel):
    """状态图执行完成后返回给调用方的结果。"""

    # request_id 与本次图执行一一对应，供调用方关联日志、问题反馈和后续请求。
    request_id: str
    # thread_id 是 LangGraph Checkpointer 的恢复主键；审批接口必须原样使用该值。
    thread_id: str
    # trace_id 是本次 HTTP/Agent Trace 的关联标识；关闭遥测时为 None。
    trace_id: str | None = None
    # execution_status 区分已经走到终点和当前因 interrupt 等待审批两种 HTTP 结果。
    execution_status: Literal["completed", "approval_required"]
    # intent 是系统识别出的有限业务意图，前端可据此选择不同展示方式。
    intent: Intent
    # intent_confidence 是分类器置信度，便于当前教学调试和后续阈值评测。
    intent_confidence: float
    # route_reason 是简短、可审计的路由依据，不包含模型详细思维过程。
    route_reason: str
    # answer 是响应节点产生的最终用户可见文本。
    answer: str
    # requires_human 表示自动化是否安全结束；True 时调用方应进入人工流程。
    requires_human: bool
    # needs_clarification 表示系统可继续自动处理，但需要用户补充订单号等必要信息。
    needs_clarification: bool
    # order_id 是订单路径成功提取的规范订单号；非订单路径或缺少参数时为 None。
    order_id: str | None = None
    # tool_name 是本次实际执行的业务工具名；没有调用工具时为 None。
    tool_name: str | None = None
    # queried_order_ids 按工具实际执行顺序返回本轮查询的全部订单号。
    queried_order_ids: list[str] = Field(default_factory=list)
    # tool_call_count 是本请求真实工具执行次数，不包含被白名单或去重门拦截的计划。
    tool_call_count: int = Field(default=0, ge=0)
    # agent_stop_reason 表示工具循环因完成、澄清或安全边界而停止。
    agent_stop_reason: str | None = None
    # citations 只在 FAQ 有充分证据时返回；订单和人工路径默认为空列表。
    citations: list[Citation] = Field(default_factory=list)
    # retrieval_score 是 FAQ 最高证据分数；非 FAQ 或未检索时为 None。
    retrieval_score: float | None = None
    # approval_required=True 表示图已安全暂停，尚未执行任何退货写工具。
    approval_required: bool = False
    # approval_request 是允许审批端查看的最小中断负载，不包含 user_id 或幂等键。
    approval_request: ApprovalRequestPayload | None = None
    # return_workflow_status 展示退货流程阶段；FAQ 和只读订单路径为 None。
    return_workflow_status: ReturnWorkflowStatus | None = None
    # return_request_id 只在批准后的写工具成功或幂等重放时存在。
    return_request_id: str | None = None
    # events 是当前教学阶段公开的执行轨迹，展示各节点按什么顺序运行。
    events: list[str]
