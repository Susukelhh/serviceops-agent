"""ServiceOps Agent 端到端离线评测数据契约、运行器和回归质量门。

本模块优先评估可以被确定性代码准确判定的行为：意图、工具轨迹、响应契约、引用和安全不变量。
主观语言质量以后可以增加人工评分或 LLM-as-judge，但不能替代这些硬性业务检查。
"""

# ceil 计算小样本 P95 的 nearest-rank 位置。
from math import ceil

# perf_counter 使用单调时钟测量每个图执行耗时，不受系统时间调整影响。
from time import perf_counter

# Literal 限制评测执行状态；Any/cast 收窄 LangGraph 框架扩展字段。
from typing import Any, Literal, cast

# uuid4 让同一数据集在同一 Checkpointer 上重复运行时仍使用隔离线程。
from uuid import uuid4

# InMemorySaver 为每次离线目标提供不写磁盘的独立 LangGraph Checkpointer。
from langgraph.checkpoint.memory import InMemorySaver

# BaseModel/Field/TypeAdapter 负责数据集、实际结果和质量门的强类型校验。
from pydantic import BaseModel, Field, TypeAdapter, model_validator

# 确定性规划器是离线零费用工具轨迹基线。
from serviceops_agent.agent.planner import DeterministicOrderToolPlanner

# 项目根路径帮助示例从任意 PyCharm Working directory 加载数据集。
from serviceops_agent.config.paths import resolve_project_path

# Settings 显式构造离线依赖，不使用用户 .env 中可能启用的真实模型后端。
from serviceops_agent.config.settings import Settings

# ToolExecutionRecord 用于从 LangGraph State 恢复真实工具执行顺序。
from serviceops_agent.domain.agent import ToolExecutionRecord

# Intent 约束人工标注只能使用项目支持的有限业务值。
from serviceops_agent.domain.enums import Intent

# Citation 是 FAQ 图允许向 API 暴露的最小证据模型。
from serviceops_agent.domain.knowledge import Citation

# ReturnWorkflowStatus 约束退货子图的人工标注终态。
from serviceops_agent.domain.returns import ReturnWorkflowStatus

# ServiceGraph/build_service_graph 负责构造与 FastAPI 使用相同的完整业务图。
from serviceops_agent.graph.builder import ServiceGraph, build_service_graph

# 关键词分类器作为稳定可比较的零费用意图基线。
from serviceops_agent.graph.nodes.classifier import classify_intent

# 默认订单仓库和隔离退货仓库让每轮评测复用真实权限/幂等业务逻辑。
from serviceops_agent.infrastructure.order_repository import default_order_repository
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    ReturnRequestRepository,
)

# 确定性摘录回答器只组织本次检索证据，不访问外部模型。
from serviceops_agent.rag.generation import ExtractiveGroundedAnswerClient

# 默认检索工厂接收显式 hash/:memory: Settings，构建完全离线 Qdrant 索引。
from serviceops_agent.rag.retriever import build_default_knowledge_retriever


class AgentEvaluationThresholds(BaseModel):
    """一套离线评测报告必须达到的聚合质量门。"""

    # overall 同时要求一条样本的四个维度都通过，默认确定性基线必须全通过。
    min_overall_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # routing 衡量有限意图是否正确。
    min_routing_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # tool_trajectory 衡量工具名、顺序、参数目标、预算和必要事件顺序。
    min_tool_trajectory_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # response_contract 衡量完成/中断、人工、澄清、引用和业务状态。
    min_response_contract_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # safety_invariant 衡量零写入、越权信息隐藏和禁止内容。
    min_safety_invariant_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    # 延迟受机器、冷启动和 CI 负载影响，默认只记录；显式配置后才作为失败门槛。
    max_p95_duration_ms: float | None = Field(default=None, gt=0.0)


class AgentEvaluationCase(BaseModel):
    """一条人工标注的 Agent 输入与可由代码确定性验证的期望行为。"""

    # case_id 是报告、pytest 参数和失败定位使用的稳定短标识。
    case_id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    # user_id 模拟验签后由 API 写入 State 的可信主体，不从 message 中解析。
    user_id: str = Field(min_length=1, max_length=64)
    # message 是送入完整 LangGraph 的人工标注问题。
    message: str = Field(min_length=1, max_length=1_000)
    # idempotency_key 只在退货写意图中需要稳定复用；缺省由运行器安全生成。
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    # expected_intent 是整图分类后必须得到的有限意图。
    expected_intent: Intent
    # expected_execution_status 区分普通终态与 interrupt 审批暂停态。
    expected_execution_status: Literal["completed", "approval_required"] = "completed"
    # expected_requires_human 验证系统是否正确进入人工、审批或自动路径。
    expected_requires_human: bool
    # expected_needs_clarification 验证调用方是否应继续收集参数。
    expected_needs_clarification: bool
    # expected_tool_names 按真实执行顺序列出工具；空列表表示严格零调用。
    expected_tool_names: list[str] = Field(default_factory=list, max_length=10)
    # expected_queried_order_ids 精确约束身份绑定订单工具实际处理顺序。
    expected_queried_order_ids: list[str] = Field(default_factory=list, max_length=10)
    # required_citation_document_ids 要求实际引用至少包含这些已发布公共文档。
    required_citation_document_ids: list[str] = Field(default_factory=list, max_length=10)
    # forbidden_citation_document_ids 确保内部/错误文档不会成为回答证据。
    forbidden_citation_document_ids: list[str] = Field(default_factory=list, max_length=10)
    # expected_return_workflow_status 只在退货子图样本中使用；其他路径应保持 None。
    expected_return_workflow_status: ReturnWorkflowStatus | None = None
    # required_answer_terms 使用稳定关键事实，而不是对整段自然语言做脆弱 exact match。
    required_answer_terms: list[str] = Field(default_factory=list, max_length=20)
    # forbidden_answer_terms 用于验证越权事实、敏感内容或幻觉不能出现在回答中。
    forbidden_answer_terms: list[str] = Field(default_factory=list, max_length=20)
    # required_event_sequence 必须按顺序出现在完整轨迹中，但允许中间增加无关观测事件。
    required_event_sequence: list[str] = Field(default_factory=list, max_length=30)
    # max_tool_call_count 是本样本允许的最高真实调用预算。
    max_tool_call_count: int = Field(default=0, ge=0, le=10)
    # expected_return_write_delta 对首次执行通常固定为零，证明审批前没有业务副作用。
    expected_return_write_delta: int = Field(default=0, ge=0, le=1)
    # tags 用于按场景筛选报告，例如 faq/order/security/approval。
    tags: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_expectation_consistency(self) -> "AgentEvaluationCase":
        """在运行图之前拒绝互相矛盾的人工标签。"""

        # 期望工具数量已经超过允许预算时，样本永远不可能通过。
        if len(self.expected_tool_names) > self.max_tool_call_count:
            raise ValueError("expected_tool_names 数量不能超过 max_tool_call_count")
        # 当前项目每次订单工具调用都对应一个目标订单，数量不一致通常表示标签漏写。
        if len(self.expected_tool_names) != len(self.expected_queried_order_ids):
            raise ValueError("expected_tool_names 与 expected_queried_order_ids 数量必须一致")
        # 审批暂停只属于退货写意图，而且审批前必须保持业务零写入。
        if self.expected_execution_status == "approval_required":
            if self.expected_intent != Intent.RETURN_REQUEST:
                raise ValueError("只有 return_request 可以期望 approval_required")
            if self.expected_return_write_delta != 0:
                raise ValueError("approval_required 样本必须期望零退货写入")
        # 同一引用或回答词不能同时被要求和禁止。
        if set(self.required_citation_document_ids) & set(
            self.forbidden_citation_document_ids
        ):
            raise ValueError("同一文档不能同时 required 和 forbidden")
        if set(self.required_answer_terms) & set(self.forbidden_answer_terms):
            raise ValueError("同一回答词不能同时 required 和 forbidden")
        # 返回已经完成跨字段检查的样本。
        return self


class AgentEvaluationDataset(BaseModel):
    """受版本控制的端到端数据集、描述和聚合质量门。"""

    # dataset_id 让报告可以明确关联数据来源。
    dataset_id: str = Field(min_length=1, max_length=100)
    # version 必须显式变更，避免新增/改标签后仍把指标当成同一基准比较。
    version: str = Field(min_length=1, max_length=30, pattern=r"^\d+\.\d+\.\d+$")
    # description 解释本数据集覆盖范围和限制。
    description: str = Field(min_length=1, max_length=500)
    # thresholds 与数据集一起版本化，CI 不依赖散落在脚本中的魔法数字。
    thresholds: AgentEvaluationThresholds
    # cases 至少包含一条样本；标准集会覆盖 FAQ、订单、未知和退货路径。
    cases: list[AgentEvaluationCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> "AgentEvaluationDataset":
        """保证同一版本内每个样本 ID 唯一。"""

        # 报告以 case_id 为主定位，重复会掩盖其中一条结果。
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Agent 评测集 case_id 不能重复")
        return self


class AgentEvaluationCaseResult(BaseModel):
    """单条图执行的实际观测、四维评分与有限失败原因。"""

    # case_id 关联人工标注样本。
    case_id: str
    # tags 原样进入报告，支持按安全/业务场景分组。
    tags: list[str]
    # routing_passed 只评价意图分类。
    routing_passed: bool
    # tool_trajectory_passed 评价工具顺序、目标、预算和事件顺序。
    tool_trajectory_passed: bool
    # response_contract_passed 评价终态、中断、人工、澄清、引用和关键事实。
    response_contract_passed: bool
    # safety_invariant_passed 评价零写入与禁止信息。
    safety_invariant_passed: bool
    # passed 只有上述四个维度全部通过才为 True。
    passed: bool
    # actual_intent 是图真实输出的有限意图字符串。
    actual_intent: str
    # actual_execution_status 根据 __interrupt__ 框架字段确定。
    actual_execution_status: Literal["completed", "approval_required"]
    # actual_requires_human/clarification 记录调用方应采取的后续动作。
    actual_requires_human: bool
    actual_needs_clarification: bool
    # actual_tool_names 来自真实 ToolExecutionRecord，而不是模型计划。
    actual_tool_names: list[str]
    # actual_queried_order_ids 来自图最终状态。
    actual_queried_order_ids: list[str]
    # actual_citation_document_ids 对 FAQ 引用按首次出现顺序去重。
    actual_citation_document_ids: list[str]
    # actual_return_workflow_status 只在退货子图存在。
    actual_return_workflow_status: str | None = None
    # actual_return_write_delta 通过仓库前后计数得到，不能只相信 State。
    actual_return_write_delta: int = Field(ge=0)
    # tool_call_count 是执行节点实际增加的调用次数。
    tool_call_count: int = Field(ge=0)
    # duration_ms 只用于观察/比较，不默认作为跨机器硬门槛。
    duration_ms: float = Field(ge=0.0)
    # answer_preview 最多保留 300 字符，只用于当前人工失败分析。
    answer_preview: str = Field(max_length=300)
    # actual_events 保存有限内部事件名，帮助区分真实轨迹错误和评测契约命名错误。
    # 事件由代码生成且不含用户原文、Token、API Key、订单内容或模型响应正文。
    actual_events: list[str] = Field(max_length=200)
    # violations 只保存稳定规则码，不复制用户原文或异常正文。
    violations: list[str]


class AgentEvaluationSummary(BaseModel):
    """一轮端到端离线实验的聚合指标、质量门与逐样本结果。"""

    # dataset_id/version 让不同报告可以正确比较，防止数据集漂移被忽略。
    dataset_id: str
    dataset_version: str
    # target_profile 标识被测依赖组合；离线基线和真实千问候选可以复用同一报告结构。
    target_profile: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    # total/passed_cases 解释所有比例的分母与分子。
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    # 以下五个比率都限制在 0 到 1。
    overall_pass_rate: float = Field(ge=0.0, le=1.0)
    routing_accuracy: float = Field(ge=0.0, le=1.0)
    tool_trajectory_accuracy: float = Field(ge=0.0, le=1.0)
    response_contract_accuracy: float = Field(ge=0.0, le=1.0)
    safety_invariant_accuracy: float = Field(ge=0.0, le=1.0)
    # 总耗时与 P95 帮助比较版本，但默认不因机器差异阻断 CI。
    total_duration_ms: float = Field(ge=0.0)
    p95_duration_ms: float = Field(ge=0.0)
    # quality_gate_passed 是脚本退出码和 CI 是否放行的唯一聚合结论。
    quality_gate_passed: bool
    # quality_gate_failures 使用稳定码说明哪一个聚合门未达到。
    quality_gate_failures: list[str]
    # results 保留逐样本失败证据，避免只看到一个无法定位的平均分。
    results: list[AgentEvaluationCaseResult]


def load_agent_evaluation_dataset(path: str) -> AgentEvaluationDataset:
    """从项目相对或绝对 UTF-8 JSON 文件加载并校验端到端数据集。"""

    # resolve_project_path 避免 PyCharm Working directory 改变相对路径含义。
    dataset_path = resolve_project_path(path)
    # 明确 UTF-8，保证 Windows 中文评测问题不会依赖系统默认编码。
    raw_json = dataset_path.read_text(encoding="utf-8")
    # TypeAdapter 校验顶层对象以及嵌套阈值/样本的全部字段。
    return TypeAdapter(AgentEvaluationDataset).validate_json(raw_json)


def build_offline_agent_evaluation_target() -> tuple[
    ServiceGraph,
    ReturnRequestRepository,
]:
    """构建不访问千问、不写磁盘且每次调用互相隔离的完整评测目标。"""

    # 所有外部能力开关显式固定，用户 .env 不能把标准离线评测改成收费网络调用。
    offline_settings = Settings(
        environment="test",
        telemetry_enabled=False,
        persistence_backend="memory",
        llm_backend="mock",
        agent_planner_backend="deterministic",
        # 工具循环预算也是被测契约的一部分，不能被开发者本机 .env 悄悄改大或改小。
        agent_max_tool_steps=3,
        embedding_backend="hash",
        # 固定 Hash 向量维度，保证不同机器和不同时间生成相同的检索空间。
        embedding_dimensions=1024,
        qdrant_location=":memory:",
        qdrant_collection=f"serviceops_agent_evaluation_{uuid4().hex}",
        # 显式固定知识源与所有检索参数，避免个人调参污染可比较的基线报告。
        knowledge_source_path="data/seed/knowledge_documents.json",
        rag_top_k=3,
        rag_score_threshold=0.10,
        # 版本1.0.0离线Agent黄金集保持第26步前的原Qdrant顺序，避免历史指标漂移。
        rag_reranker="off",
        rag_chunk_size=500,
        rag_chunk_overlap=80,
        rag_generation_backend="extractive",
        # 摘录后端不会调用模型，但仍固定上下文预算以保持配置语义完整。
        rag_max_context_chars=4000,
    )
    # 退货仓库属于本轮实验，前后 count 可以验证审批前零写入。
    return_repository = InMemoryReturnRequestRepository(default_order_repository)
    # 显式 hash/:memory: 参数构建受治理知识索引，不读取真实模型密钥。
    knowledge_retriever = build_default_knowledge_retriever(offline_settings)
    # 编译与 API 相同节点/边的完整图，只替换为确定性依赖和内存 Checkpointer。
    graph = build_service_graph(
        classifier_node=classify_intent,
        order_repository=default_order_repository,
        knowledge_retriever=knowledge_retriever,
        faq_answer_client=ExtractiveGroundedAnswerClient(),
        tool_planner=DeterministicOrderToolPlanner(),
        return_request_repository=return_repository,
        faq_top_k=offline_settings.rag_top_k,
        agent_max_tool_steps=offline_settings.agent_max_tool_steps,
        checkpointer=InMemorySaver(),
    )
    # 返回图和同一图绑定的仓库；调用方不得另建仓库做副作用计数。
    return graph, return_repository


def _is_subsequence(required: list[str], actual: list[str]) -> bool:
    """判断 required 是否按顺序出现在 actual 中，允许中间存在其他事件。"""

    # 空期望天然满足，避免调用方为不关心轨迹的样本填写占位符。
    if not required:
        return True
    # required_index 指向下一项尚未匹配的期望事件。
    required_index = 0
    # 按真实执行顺序扫描一次即可，复杂度为 O(n)。
    for event in actual:
        if event == required[required_index]:
            required_index += 1
            if required_index == len(required):
                return True
    return False


def _actual_tool_names(result: dict[str, Any]) -> list[str]:
    """从 State 中强类型恢复实际工具记录，并保持真实执行顺序。"""

    # 没有进入订单工具循环的路径自然使用空列表。
    raw_records = result.get("tool_execution_records", [])
    if not isinstance(raw_records, list):
        return []
    # 每个元素都重新经过领域 Schema，Checkpoint JSON 字典和内存对象都能统一处理。
    records: list[ToolExecutionRecord] = []
    for raw_record in raw_records:
        try:
            records.append(ToolExecutionRecord.model_validate(raw_record))
        except Exception:
            # 损坏记录会让实际轨迹缺项，随后由 exact match 产生明确评测失败。
            continue
    return [record.tool_name for record in records]


def _actual_citation_document_ids(result: dict[str, Any]) -> list[str]:
    """从 State 引用恢复并保序去重文档 ID。"""

    raw_citations = result.get("citations", [])
    if not isinstance(raw_citations, list):
        return []
    document_ids: list[str] = []
    for raw_citation in raw_citations:
        try:
            citation = Citation.model_validate(raw_citation)
        except Exception:
            continue
        document_ids.append(citation.document_id)
    # dict.fromkeys 保留第一条切片引用的排名顺序，同时去除同文档多切片。
    return list(dict.fromkeys(document_ids))


def _append_violation(
    violations: list[str],
    condition: bool,
    code: str,
) -> bool:
    """条件失败时追加稳定规则码，并返回原条件供维度聚合。"""

    if not condition:
        violations.append(code)
    return condition


async def evaluate_agent_dataset(
    graph: ServiceGraph,
    return_repository: ReturnRequestRepository,
    dataset: AgentEvaluationDataset,
    *,
    target_profile: str = "offline_deterministic",
) -> AgentEvaluationSummary:
    """逐条运行完整 LangGraph，并计算四类确定性指标与聚合质量门。"""

    # results 按数据集顺序保存，使 JSON 报告与 Git diff 稳定。
    results: list[AgentEvaluationCaseResult] = []
    # 数据集级计时包含所有图执行，但不包含读取 JSON 和构建索引。
    evaluation_started_at = perf_counter()

    for case in dataset.cases:
        # 每条样本使用不同 UUID 线程，避免 Checkpoint 状态串扰。
        execution_id = str(uuid4())
        # 仓库实际计数比 State 标志更能证明是否产生副作用。
        return_count_before = return_repository.count()
        # 单样本计时覆盖完整图执行与 interrupt 保存。
        case_started_at = perf_counter()
        raw_result = await graph.ainvoke(
            {
                "request_id": f"eval-{execution_id}",
                "user_id": case.user_id,
                "user_message": case.message,
                "idempotency_key": case.idempotency_key or f"eval-{execution_id}",
                "events": ["evaluation:request_received"],
            },
            config={"configurable": {"thread_id": f"eval-thread-{execution_id}"}},
        )
        duration_ms = (perf_counter() - case_started_at) * 1_000
        # ServiceState 类型没有声明框架 __interrupt__；cast 只用于读取该扩展字段。
        result = cast(dict[str, Any], raw_result)
        return_write_delta = return_repository.count() - return_count_before

        # 收集实际输出时只保留评测需要的受控字段。
        actual_intent = str(result.get("intent", "missing"))
        actual_execution_status: Literal["completed", "approval_required"] = (
            "approval_required" if result.get("__interrupt__") else "completed"
        )
        actual_requires_human = result.get("requires_human") is True
        actual_needs_clarification = result.get("needs_clarification") is True
        actual_tool_names = _actual_tool_names(result)
        raw_order_ids = result.get("queried_order_ids", [])
        actual_queried_order_ids = (
            [str(order_id) for order_id in raw_order_ids]
            if isinstance(raw_order_ids, list)
            else []
        )
        actual_citation_document_ids = _actual_citation_document_ids(result)
        raw_workflow_status = result.get("return_workflow_status")
        actual_workflow_status = (
            str(raw_workflow_status) if raw_workflow_status is not None else None
        )
        raw_tool_call_count = result.get("tool_call_count", 0)
        tool_call_count = (
            raw_tool_call_count if isinstance(raw_tool_call_count, int) else 0
        )
        answer = str(result.get("answer", ""))
        raw_events = result.get("events", [])
        actual_events = (
            [str(event) for event in raw_events]
            if isinstance(raw_events, list)
            else []
        )

        # 每个维度先独立收集规则结果，便于看到“总失败”究竟来自哪一层。
        violations: list[str] = []
        routing_checks = [
            _append_violation(
                violations,
                actual_intent == case.expected_intent.value,
                "intent_mismatch",
            )
        ]
        trajectory_checks = [
            _append_violation(
                violations,
                actual_tool_names == case.expected_tool_names,
                "tool_sequence_mismatch",
            ),
            _append_violation(
                violations,
                actual_queried_order_ids == case.expected_queried_order_ids,
                "queried_order_sequence_mismatch",
            ),
            _append_violation(
                violations,
                tool_call_count <= case.max_tool_call_count,
                "tool_budget_exceeded",
            ),
            _append_violation(
                violations,
                _is_subsequence(case.required_event_sequence, actual_events),
                "required_event_sequence_missing",
            ),
        ]
        contract_checks = [
            _append_violation(
                violations,
                actual_execution_status == case.expected_execution_status,
                "execution_status_mismatch",
            ),
            _append_violation(
                violations,
                actual_requires_human == case.expected_requires_human,
                "requires_human_mismatch",
            ),
            _append_violation(
                violations,
                actual_needs_clarification == case.expected_needs_clarification,
                "needs_clarification_mismatch",
            ),
            _append_violation(
                violations,
                actual_workflow_status
                == (
                    case.expected_return_workflow_status.value
                    if case.expected_return_workflow_status is not None
                    else None
                ),
                "return_workflow_status_mismatch",
            ),
            _append_violation(
                violations,
                set(case.required_citation_document_ids).issubset(
                    actual_citation_document_ids
                ),
                "required_citation_missing",
            ),
            _append_violation(
                violations,
                all(term in answer for term in case.required_answer_terms),
                "required_answer_term_missing",
            ),
        ]
        safety_checks = [
            _append_violation(
                violations,
                return_write_delta == case.expected_return_write_delta,
                "return_write_delta_mismatch",
            ),
            _append_violation(
                violations,
                not (
                    set(case.forbidden_citation_document_ids)
                    & set(actual_citation_document_ids)
                ),
                "forbidden_citation_present",
            ),
            _append_violation(
                violations,
                all(term not in answer for term in case.forbidden_answer_terms),
                "forbidden_answer_term_present",
            ),
            _append_violation(
                violations,
                not (
                    actual_execution_status == "approval_required"
                    and result.get("return_request_id") is not None
                ),
                "approval_pending_contains_write_result",
            ),
        ]

        # all(...) 保留每个维度严格“全部规则通过”的语义。
        routing_passed = all(routing_checks)
        tool_trajectory_passed = all(trajectory_checks)
        response_contract_passed = all(contract_checks)
        safety_invariant_passed = all(safety_checks)
        passed = all(
            (
                routing_passed,
                tool_trajectory_passed,
                response_contract_passed,
                safety_invariant_passed,
            )
        )
        results.append(
            AgentEvaluationCaseResult(
                case_id=case.case_id,
                tags=case.tags,
                routing_passed=routing_passed,
                tool_trajectory_passed=tool_trajectory_passed,
                response_contract_passed=response_contract_passed,
                safety_invariant_passed=safety_invariant_passed,
                passed=passed,
                actual_intent=actual_intent,
                actual_execution_status=actual_execution_status,
                actual_requires_human=actual_requires_human,
                actual_needs_clarification=actual_needs_clarification,
                actual_tool_names=actual_tool_names,
                actual_queried_order_ids=actual_queried_order_ids,
                actual_citation_document_ids=actual_citation_document_ids,
                actual_return_workflow_status=actual_workflow_status,
                actual_return_write_delta=return_write_delta,
                tool_call_count=tool_call_count,
                duration_ms=duration_ms,
                answer_preview=answer[:300],
                # 保留本次真实事件顺序；报告失败后无需付费重跑即可定位缺失或乱序位置。
                actual_events=actual_events,
                violations=violations,
            )
        )

    # 聚合分母固定为经过数据集 Schema 保证非空的样本数。
    total_cases = len(results)
    passed_cases = sum(result.passed for result in results)
    overall_pass_rate = passed_cases / total_cases
    routing_accuracy = sum(result.routing_passed for result in results) / total_cases
    tool_trajectory_accuracy = (
        sum(result.tool_trajectory_passed for result in results) / total_cases
    )
    response_contract_accuracy = (
        sum(result.response_contract_passed for result in results) / total_cases
    )
    safety_invariant_accuracy = (
        sum(result.safety_invariant_passed for result in results) / total_cases
    )
    # nearest-rank P95 对小样本也有确定义；至少一条结果使索引始终合法。
    sorted_durations = sorted(result.duration_ms for result in results)
    p95_index = ceil(0.95 * len(sorted_durations)) - 1
    p95_duration_ms = sorted_durations[p95_index]
    total_duration_ms = (perf_counter() - evaluation_started_at) * 1_000

    # 与数据集版本绑定的聚合门逐项比较，失败只保存稳定码。
    thresholds = dataset.thresholds
    quality_gate_failures: list[str] = []
    if overall_pass_rate < thresholds.min_overall_pass_rate:
        quality_gate_failures.append("overall_pass_rate_below_threshold")
    if routing_accuracy < thresholds.min_routing_accuracy:
        quality_gate_failures.append("routing_accuracy_below_threshold")
    if tool_trajectory_accuracy < thresholds.min_tool_trajectory_accuracy:
        quality_gate_failures.append("tool_trajectory_accuracy_below_threshold")
    if response_contract_accuracy < thresholds.min_response_contract_accuracy:
        quality_gate_failures.append("response_contract_accuracy_below_threshold")
    if safety_invariant_accuracy < thresholds.min_safety_invariant_accuracy:
        quality_gate_failures.append("safety_invariant_accuracy_below_threshold")
    if (
        thresholds.max_p95_duration_ms is not None
        and p95_duration_ms > thresholds.max_p95_duration_ms
    ):
        quality_gate_failures.append("p95_duration_above_threshold")

    # 返回完整报告；调用脚本根据 quality_gate_passed 决定进程退出码。
    return AgentEvaluationSummary(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.version,
        # 调用方明确命名当前目标，报告才能区分确定性基线和真实模型候选。
        target_profile=target_profile,
        total_cases=total_cases,
        passed_cases=passed_cases,
        overall_pass_rate=overall_pass_rate,
        routing_accuracy=routing_accuracy,
        tool_trajectory_accuracy=tool_trajectory_accuracy,
        response_contract_accuracy=response_contract_accuracy,
        safety_invariant_accuracy=safety_invariant_accuracy,
        total_duration_ms=total_duration_ms,
        p95_duration_ms=p95_duration_ms,
        quality_gate_passed=not quality_gate_failures,
        quality_gate_failures=quality_gate_failures,
        results=results,
    )
