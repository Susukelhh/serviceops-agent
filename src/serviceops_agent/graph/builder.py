"""装配 ServiceOps 状态图。

把图结构集中放在一个文件中，可以让面试官或维护者快速看清节点和边，
也能避免每个节点反向依赖整张图。
"""

# isawaitable 让统一包装节点可以同时执行同步关键词基线和异步真实模型节点。
from inspect import isawaitable

# BaseCheckpointSaver 抽象持久化能力；InMemorySaver 用于当前单进程开发审批恢复。
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

# START/END 是图的虚拟起点和终点；StateGraph 是声明节点与边的构建器。
from langgraph.graph import END, START, StateGraph

# CompiledStateGraph 是图编译后的可执行类型，用于精确标注函数返回值。
from langgraph.graph.state import CompiledStateGraph

# ToolPlanner 与工厂支持默认确定性规划、真实 LLM 规划和故障测试替身。
from serviceops_agent.agent.planner import ToolPlanner, create_tool_planner

# get_settings 只用于读取 FAQ Top-K；模型和检索器仍由各自工厂构建。
from serviceops_agent.config.settings import get_settings

# Intent 用于在写入 Checkpoint 前把 StrEnum 转换为可移植的 JSON 字符串。
from serviceops_agent.domain.enums import Intent

# FAQ 节点分别负责检索证据和只基于证据生成回答。
from serviceops_agent.graph.nodes.faq import (
    create_faq_answer_node,
    create_faq_retrieval_node,
)

# 导入输入预处理节点：它负责清理原始用户文本，但不负责业务判断。
from serviceops_agent.graph.nodes.intake import normalize_request

# 订单 Agent 节点分别负责初始化、规划、受控执行、观察汇总和参数澄清。
from serviceops_agent.graph.nodes.order import (
    clarify_order_request,
    create_order_planning_node,
    create_order_tool_execution_node,
    finalize_order_agent_response,
    initialize_order_agent,
)

# 导入人工接管终点响应节点。
from serviceops_agent.graph.nodes.responders import handoff_to_human

# 退货节点把草案、interrupt 审批、幂等写工具和拒绝终点显式分开。
from serviceops_agent.graph.nodes.returns import (
    create_return_request_execution_node,
    create_return_request_proposal_node,
    finalize_return_rejection,
    request_return_approval,
)

# 导入条件路由函数：它把当前 Intent 转换为下一跳使用的路由键。
from serviceops_agent.graph.routes import (
    select_faq_answer_path,
    select_faq_evidence_path,
    select_order_execution_path,
    select_order_plan_path,
    select_response_path,
    select_return_approval_path,
    select_return_proposal_path,
)

# 导入共享状态结构，让 StateGraph 知道节点之间允许传递哪些字段。
from serviceops_agent.graph.state import ServiceState

# 显式 Checkpoint 类型白名单允许教学调试安全恢复项目强类型 State。
from serviceops_agent.infrastructure.checkpoint_serde import create_checkpoint_serializer

# 导入仓库协议和默认 JSON 模拟仓库，测试可以注入独立内存数据。
from serviceops_agent.infrastructure.order_repository import (
    OrderRepository,
    default_order_repository,
)

# 退货写仓库协议与进程内实现支持依赖注入、幂等测试和 API 单进程演示。
from serviceops_agent.infrastructure.return_repository import (
    InMemoryReturnRequestRepository,
    ReturnRequestRepository,
)

# 导入分类节点类型和工厂，使图能根据配置选择关键词基线或真实模型。
from serviceops_agent.llm.factory import IntentClassifierNode, build_intent_classifier_node

# instrument_graph_node 为每个业务节点创建父子 Span 和有限低基数耗时指标。
from serviceops_agent.observability.telemetry import instrument_graph_node

# GroundedAnswerClient 让图可注入确定性基线、真实千问生成器或故障测试替身。
from serviceops_agent.rag.generation import (
    GroundedAnswerClient,
    create_grounded_answer_client,
)

# 查询范围策略在FAQ节点调用Embedding前执行可审计的业务边界判断。
from serviceops_agent.rag.query_policy import (
    KnowledgeQueryPolicy,
    create_knowledge_query_policy,
)

# 默认 Qdrant 检索器和协议支持生产装配与测试替身依赖注入。
from serviceops_agent.rag.retriever import (
    KnowledgeRetriever,
    build_default_knowledge_retriever,
)

# ServiceGraph 统一图工厂、FastAPI State 和运行时资源的复杂泛型类型。
type ServiceGraph = CompiledStateGraph[ServiceState, None, ServiceState, ServiceState]


def build_service_graph(
    classifier_node: IntentClassifierNode | None = None,
    order_repository: OrderRepository | None = None,
    knowledge_retriever: KnowledgeRetriever | None = None,
    retrieval_event: str | None = None,
    faq_query_policy: KnowledgeQueryPolicy | None = None,
    faq_answer_client: GroundedAnswerClient | None = None,
    tool_planner: ToolPlanner | None = None,
    return_request_repository: ReturnRequestRepository | None = None,
    faq_top_k: int | None = None,
    agent_max_tool_steps: int | None = None,
    checkpointer: BaseCheckpointSaver[str] | None = None,
) -> ServiceGraph:
    """构建并编译第一版工单状态图。

    LangGraph 的节点函数只返回自己修改的那部分状态。框架会把这些增量合并到共享状态，
    其中 `events` 字段通过 Reducer 进行追加，而不是被后一个节点覆盖。
    """

    # 一次性读取集中配置，避免不同依赖工厂分别读取而隐藏装配关系。
    current_settings = get_settings()
    # 显式传入节点时用于测试；正常启动则根据 LLM_BACKEND 配置选择分类实现。
    selected_classifier_node = classifier_node or build_intent_classifier_node()
    # 显式传入仓库时用于隔离测试；正常启动使用从 JSON 加载的模拟仓库。
    selected_order_repository = order_repository or default_order_repository
    # 每张图默认创建独立退货仓库；API 长生命周期图会持续复用该实例。
    selected_return_request_repository = (
        return_request_repository
        if return_request_repository is not None
        else InMemoryReturnRequestRepository(selected_order_repository)
    )
    # FAQ 检索器支持测试注入；正常启动构建 Qdrant 与受治理知识索引。
    selected_knowledge_retriever = knowledge_retriever or build_default_knowledge_retriever()
    # 查询范围策略支持测试注入；默认依据版本化配置在Embedding前执行拒绝判断。
    selected_faq_query_policy = faq_query_policy or create_knowledge_query_policy(
        current_settings.rag_query_policy
    )
    # FAQ 回答客户端支持测试注入；正常启动依据配置选择确定性摘录或真实模型生成。
    selected_faq_answer_client = faq_answer_client or create_grounded_answer_client(
        current_settings
    )
    # 工具规划器支持测试注入；默认依据配置选择确定性基线或真实模型。
    selected_tool_planner = tool_planner or create_tool_planner(current_settings)
    # 显式 Top-K 便于检索测试；正常启动读取集中配置。
    selected_faq_top_k = faq_top_k if faq_top_k is not None else current_settings.rag_top_k
    # 显式最大步数用于边界测试；生产默认读取集中配置。
    selected_max_tool_steps = (
        agent_max_tool_steps
        if agent_max_tool_steps is not None
        else current_settings.agent_max_tool_steps
    )
    # 最大步数即使通过函数参数注入也必须保持 Settings 相同的安全范围。
    if not 1 <= selected_max_tool_steps <= 10:
        # 图装配阶段立即拒绝无界或异常预算。
        raise ValueError("agent_max_tool_steps 必须在 1 到 10 之间")
    # 创建绑定规划器与最大步数的异步规划节点。
    order_planning_node = create_order_planning_node(
        # 注入可替换规划协议。
        selected_tool_planner,
        # 规划前会检查是否已经耗尽执行预算。
        max_tool_steps=selected_max_tool_steps,
    )
    # 创建绑定可信仓库与同一最大步数的工具执行节点，形成纵深防御。
    order_tool_execution_node = create_order_tool_execution_node(
        # 仓库仍由系统装配，模型不能替换。
        selected_order_repository,
        # 执行前再次检查预算，不能只依赖规划节点。
        max_tool_steps=selected_max_tool_steps,
    )
    # 草案节点只执行订单归属和状态预检查，不产生写操作。
    return_request_proposal_node = create_return_request_proposal_node(selected_order_repository)
    # 写节点只有在 interrupt 恢复为明确批准后才会被条件边调用。
    return_request_execution_node = create_return_request_execution_node(
        selected_return_request_repository
    )
    # 创建已经绑定 Top-K 配置的 FAQ 检索节点；默认值来自缓存 Settings。
    # 只有默认工厂真实装配BM25时才公开重排事件；测试替身不能冒充执行过重排。
    selected_rerank_event = retrieval_event or (
        # 使用低基数稳定事件名供控制台和评测读取。
        (
            "graph:faq_candidates_fused_rrf"
            if current_settings.rag_reranker == "hybrid_rrf"
            else (
                "graph:faq_candidates_reranked_cross_encoder"
                if current_settings.rag_reranker == "cross_encoder"
                else "graph:faq_candidates_reranked_bm25"
            )
        )
        # 只有工厂内部真实装配重排/融合时，才自动公开对应事件。
        if knowledge_retriever is None and current_settings.rag_reranker != "off"
        # 自定义检索器或关闭模式不追加事件。
        else None
    )
    faq_retrieval_node = create_faq_retrieval_node(
        # 注入可替换知识检索协议。
        selected_knowledge_retriever,
        # 默认检索器已使用配置阈值；节点只需要限制候选数量。
        top_k=selected_faq_top_k,
        # 范围策略先于真实检索运行，避免域外请求产生Embedding费用和错误证据。
        query_policy=selected_faq_query_policy,
        # 公开事件解释候选顺序发生了可审计BM25融合，不包含查询或知识正文。
        rerank_event=selected_rerank_event,
    )
    # 创建已经绑定具体生成客户端的异步节点；节点内部执行引用白名单校验。
    faq_answer_node = create_faq_answer_node(selected_faq_answer_client)

    async def run_selected_classifier(state: ServiceState) -> dict[str, object]:
        """用统一异步签名执行同步基线或异步 LLM 分类节点。"""

        # 调用注入的分类节点；返回值可能是立即得到的字典，也可能是等待模型的 Awaitable。
        classifier_result = selected_classifier_node(state)
        # 真实模型节点返回 Awaitable，必须等待完成后再把状态增量交给 LangGraph。
        if isawaitable(classifier_result):
            # await 后得到与同步基线相同的部分状态字典。
            classifier_update = await classifier_result
        else:
            # 同步关键词基线已经直接返回字典，无需额外调度。
            classifier_update = classifier_result
        # 读取分类器返回的强类型枚举。
        selected_intent = classifier_update.get("intent")
        # 持久化图只保存 JSON 友好的字符串，路由仍按同一枚举值判断。
        if isinstance(selected_intent, Intent):
            # 原字典属于本节点结果，可以安全地把枚举替换为 value。
            classifier_update["intent"] = selected_intent.value
        # 返回已经适合 Checkpointer 序列化的分类状态。
        return classifier_update

    async def run_order_planning(state: ServiceState) -> dict[str, object]:
        """等待规划器根据最新工具观察选择下一步有限动作。"""

        # 确定性与真实 LLM 规划器共用异步协议，因此图结构无需区分后端。
        return await order_planning_node(state)

    def run_order_tool_execution(state: ServiceState) -> dict[str, object]:
        """调用绑定仓库的受控工具执行器，并写入强类型观察历史。"""

        # 包装层只稳定 LangGraph 泛型推断，不修改执行器安全逻辑。
        return order_tool_execution_node(state)

    def run_return_request_proposal(state: ServiceState) -> dict[str, object]:
        """执行退货审批前只读校验并生成强类型草案。"""

        # 包装层不修改草案或安全判断。
        return return_request_proposal_node(state)

    def run_return_request_execution(state: ServiceState) -> dict[str, object]:
        """在批准后执行绑定身份和幂等仓库的退货写工具。"""

        # 真正写入只发生在该包装节点调用的执行器内部。
        return return_request_execution_node(state)

    def run_faq_retrieval(state: ServiceState) -> dict[str, object]:
        """使用明确函数签名调用已注入检索器的 FAQ 闭包节点。"""

        # 包装层只解决 LangGraph 泛型推断，不改变检索节点返回的任何状态字段。
        return faq_retrieval_node(state)

    async def run_faq_answer(state: ServiceState) -> dict[str, object]:
        """等待受约束 FAQ 生成节点，并返回经过引用白名单验证的状态增量。"""

        # 无论使用确定性基线还是真实千问，协议都采用异步签名以适配 FastAPI。
        return await faq_answer_node(state)

    # 创建“尚未编译”的图构建器，并指定整张图使用 ServiceState 作为共享状态。
    graph_builder: StateGraph[ServiceState, None, ServiceState, ServiceState] = StateGraph(
        ServiceState
    )

    # 节点名是执行轨迹、日志和后续评测中的稳定标识，因此使用清晰的业务动作命名。
    # 注册输入规范化节点；后续边通过字符串 `normalize_request` 引用它。
    graph_builder.add_node(
        "normalize_request",
        instrument_graph_node("normalize_request", normalize_request),
    )
    # 注册意图分类节点；它必须在输入规范化之后执行。
    graph_builder.add_node(
        "classify_intent",
        instrument_graph_node("classify_intent", run_selected_classifier),
    )
    # 注册 FAQ 检索节点；它负责向量查询、证据阈值和引用构建。
    graph_builder.add_node(
        "retrieve_faq",
        instrument_graph_node("retrieve_faq", run_faq_retrieval),
    )
    # 注册 grounded FAQ 回答节点；它只能读取已经通过阈值的检索证据。
    graph_builder.add_node(
        "answer_faq",
        instrument_graph_node("answer_faq", run_faq_answer),
    )
    # 注册订单 Agent 初始化节点；每个请求从零计数和空观察历史开始。
    graph_builder.add_node(
        "initialize_order_agent",
        instrument_graph_node("initialize_order_agent", initialize_order_agent),
    )
    # 注册异步规划节点；每一轮只提出一个工具调用或停止动作。
    graph_builder.add_node(
        "plan_order_action",
        instrument_graph_node("plan_order_action", run_order_planning),
    )
    # 注册唯一工具执行边界；白名单、身份、步数、去重和结果校验都在这里完成。
    graph_builder.add_node(
        "execute_order_tool",
        instrument_graph_node("execute_order_tool", run_order_tool_execution),
    )
    # 注册确定性结果汇总节点；它不允许模型改写订单和物流事实。
    graph_builder.add_node(
        "finalize_order_response",
        instrument_graph_node(
            "finalize_order_response",
            finalize_order_agent_response,
        ),
    )
    # 注册缺参数澄清节点；没有订单号时不会调用任何工具。
    graph_builder.add_node(
        "clarify_order_request",
        instrument_graph_node("clarify_order_request", clarify_order_request),
    )
    # 注册退货草案节点；它只做读取和校验，不执行写工具。
    graph_builder.add_node(
        "prepare_return_request",
        instrument_graph_node("prepare_return_request", run_return_request_proposal),
    )
    # 注册可恢复人工审批节点；首次执行会调用 interrupt 暂停图。
    graph_builder.add_node(
        "request_return_approval",
        instrument_graph_node("request_return_approval", request_return_approval),
    )
    # 注册唯一退货写执行节点；条件边和节点内部都会复查 approved=True。
    graph_builder.add_node(
        "execute_return_request",
        instrument_graph_node("execute_return_request", run_return_request_execution),
    )
    # 注册人工拒绝终点；该节点不创建写工具。
    graph_builder.add_node(
        "finalize_return_rejection",
        instrument_graph_node(
            "finalize_return_rejection",
            finalize_return_rejection,
        ),
    )
    # 注册人工接管节点；所有无法可靠自动处理的请求都会进入这里。
    graph_builder.add_node(
        "handoff_to_human",
        instrument_graph_node("handoff_to_human", handoff_to_human),
    )

    # START 与 END 是框架提供的虚拟节点，不包含业务代码。
    # 声明图启动后首先执行输入规范化节点。
    graph_builder.add_edge(START, "normalize_request")
    # 声明规范化完成后固定进入意图分类节点，这是一个无条件顺序边。
    graph_builder.add_edge("normalize_request", "classify_intent")

    # 条件边读取当前状态并返回一个路由键，再通过映射选择真正执行的节点。
    graph_builder.add_conditional_edges(
        # `classify_intent` 执行完成后才进行这次条件判断。
        "classify_intent",
        # 路由函数读取最新共享状态，并返回 faq/order/human 中的一个字符串。
        select_response_path,
        # 映射表把路由函数返回的短键转换成图中已经注册的真实节点名。
        {
            # `faq` 路由键先进入知识检索，而不是直接生成回答。
            "faq": "retrieve_faq",
            # `order` 路由键先初始化受控工具循环，而不是直接调用工具。
            "order": "initialize_order_agent",
            # `return_request` 先准备审批草案，不能直接跳到写工具。
            "return_request": "prepare_return_request",
            # `human` 路由键进入人工接管节点。
            "human": "handoff_to_human",
        },
    )

    # FAQ 检索完成后再执行第二次条件判断：有证据才能回答，无证据必须转人工。
    graph_builder.add_conditional_edges(
        # 检索节点已经写入 has_sufficient_evidence 和 retrieval_hits。
        "retrieve_faq",
        # 证据路由函数采用缺失即人工的安全默认策略。
        select_faq_evidence_path,
        # 固定路由键映射到 grounded answer 或人工节点。
        {
            # 只有达到阈值的已发布证据才能进入回答节点。
            "answer": "answer_faq",
            # 无证据、低分或检索故障统一进入人工接管。
            "human": "handoff_to_human",
        },
    )

    # 生成节点执行后进行第三次条件判断：只有答案和引用都通过校验才能结束。
    graph_builder.add_conditional_edges(
        # answer_faq 已写入 faq_answer_grounded、answer 和最终 citations。
        "answer_faq",
        # 路由函数采用缺失即人工的安全默认策略。
        select_faq_answer_path,
        # 成功答案结束执行；任何生成或引用异常进入人工节点覆盖用户文案。
        {
            # complete 表示最终答案已通过引用候选白名单检查。
            "complete": END,
            # human 表示禁止返回生成草稿，由人工接管节点生成安全说明。
            "human": "handoff_to_human",
        },
    )

    # 订单初始化后固定进入第一轮规划。
    graph_builder.add_edge("initialize_order_agent", "plan_order_action")
    # 每轮规划通过有限动作条件边选择执行、汇总、澄清或人工节点。
    graph_builder.add_conditional_edges(
        # 规划器已经写入 agent_next_action 和可选 planned_tool_call。
        "plan_order_action",
        # 路由函数不执行业务，只把安全动作映射为真实节点。
        select_order_plan_path,
        # 四个键覆盖工具循环的全部合法出口。
        {
            # execute 进入唯一工具执行边界。
            "execute": "execute_order_tool",
            # finalize 进入确定性多结果汇总。
            "finalize": "finalize_order_response",
            # clarify 在零工具调用时追问订单号。
            "clarify": "clarify_order_request",
            # human 处理模型故障、越权计划、预算耗尽等安全退出。
            "human": "handoff_to_human",
        },
    )
    # 工具执行后只有成功观察可以回到规划器，其他结果立即转人工。
    graph_builder.add_conditional_edges(
        # execute_order_tool 已写入执行成功标记和下一步动作。
        "execute_order_tool",
        # 执行路由采用缺失即人工的安全默认策略。
        select_order_execution_path,
        # continue 构成显式循环回边；human 是异常出口。
        {
            # 成功后让规划器观察结果并决定下一个工具或结束。
            "continue": "plan_order_action",
            # 失败时禁止自动重试，直接进入人工。
            "human": "handoff_to_human",
        },
    )
    # 规划器 finish 且结果汇总完成后结束本轮图执行。
    graph_builder.add_edge("finalize_order_response", END)
    # 参数澄清响应生成后结束，等待用户在下一轮补充信息。
    graph_builder.add_edge("clarify_order_request", END)

    # 退货草案准备后决定创建 interrupt、直接结束或异常转人工。
    graph_builder.add_conditional_edges(
        # prepare 节点已经写入强类型草案或确定性拒绝/澄清状态。
        "prepare_return_request",
        # 路由函数采用缺失即人工的安全默认值。
        select_return_proposal_path,
        # approval 是唯一会进入中断节点的路径。
        {
            # 草案完整且资格通过时等待外部审批。
            "approval": "request_return_approval",
            # 缺参数或业务拒绝已有 answer，可以结束。
            "complete": END,
            # 状态异常进入统一人工响应。
            "human": "handoff_to_human",
        },
    )
    # interrupt 恢复后根据强类型审批决定选择写入、拒绝或异常人工。
    graph_builder.add_conditional_edges(
        # 首次运行会暂停在该节点，不会执行本条件边；恢复后才继续。
        "request_return_approval",
        # 决定和值状态必须同时一致。
        select_return_approval_path,
        # 只有 execute 可以到达写工具。
        {
            # approved=True 进入幂等写节点。
            "execute": "execute_return_request",
            # approved=False 进入零写入拒绝节点。
            "reject": "finalize_return_rejection",
            # 无效恢复值或状态矛盾转人工。
            "human": "handoff_to_human",
        },
    )
    # 写工具完成成功、业务拒绝或安全失败后结束本轮恢复执行。
    graph_builder.add_edge("execute_return_request", END)
    # 人工拒绝响应完成后结束，仓库保持零新增。
    graph_builder.add_edge("finalize_return_rejection", END)
    # 人工接管节点生成转接说明后，本轮状态图执行结束。
    graph_builder.add_edge("handoff_to_human", END)

    # compile 会校验节点和边；传入 Checkpointer 后 interrupt 才能保存并恢复状态。
    return graph_builder.compile(checkpointer=checkpointer)


# 无 Checkpointer 图保留给不触发 interrupt 的纯单元/集成测试，调用时不要求 thread_id。
stateless_service_graph = build_service_graph()

# 该内存图只作为不触发 FastAPI lifespan 的轻量 API 测试后备。
# Uvicorn 正常启动时，runtime.py 会按配置用全新的内存图或 SQLite 持久化图覆盖它。
service_graph = build_service_graph(
    checkpointer=InMemorySaver(serde=create_checkpoint_serializer())
)
