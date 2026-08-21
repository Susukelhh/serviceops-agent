"""完整状态图的集成测试。"""

# pytest 的 asyncio 标记允许测试函数 await LangGraph 的异步执行接口。
import pytest

# AgentAction/ToolCallPlan/ToolExecutionRecord 用于构造受控规划器测试替身。
from serviceops_agent.domain.agent import AgentAction, ToolCallPlan, ToolExecutionRecord

# IntentClassification/Intent 用于断言完整图最终写入的业务分类结果。
from serviceops_agent.domain.classification import IntentClassification
from serviceops_agent.domain.enums import Intent

# GroundedAnswerDraft 是生成测试替身的结构化输出；RetrievalHit 是检索协议返回类型。
from serviceops_agent.domain.knowledge import GroundedAnswerDraft, RetrievalHit

# OrderRecord 是故障仓库协议方法的返回类型。
from serviceops_agent.domain.orders import OrderRecord

# stateless_service_graph 是不要求 thread_id 的只读集成测试图；审批测试会单独注入 Saver。
from serviceops_agent.graph.builder import build_service_graph
from serviceops_agent.graph.builder import stateless_service_graph as service_graph

# 内部模型错误和节点工厂用于构造不访问网络的故障降级集成图。
from serviceops_agent.llm.errors import LLMFailureKind, LLMServiceError
from serviceops_agent.llm.intent_classifier import create_llm_intent_classifier_node


class UnavailableClassificationClient:
    """模拟模型连接失败的客户端，用于验证完整 LangGraph 降级路径。"""

    async def classify(self, message: str) -> IntentClassification:
        """每次调用都抛出已归一化连接错误。"""

        # 显式引用输入，保持替身与真实客户端具有相同调用语义。
        _ = message
        # 连接故障标记为可稍后重试，但当前请求必须立即安全转人工。
        raise LLMServiceError(LLMFailureKind.CONNECTION, retryable=True)


class EmptyKnowledgeRetriever:
    """始终返回无证据结果的检索替身，用于验证 RAG 拒答路径。"""

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """接收合法查询参数但不返回任何知识命中。"""

        # 显式引用两个参数，说明替身遵循真实 KnowledgeRetriever 协议。
        _ = (query, top_k)
        # 空列表表示基础设施正常，但知识覆盖或相关度不足。
        return []


class FabricatedCitationAnswerClient:
    """模拟回答正文看似正常、但引用了候选集合外 ID 的不可信生成器。"""

    async def generate(
        self,
        *,
        question: str,
        evidence: list[RetrievalHit],
    ) -> GroundedAnswerDraft:
        """返回伪造引用，验证确定性白名单不会相信模型自报结果。"""

        # 显式读取参数以说明替身遵循 GroundedAnswerClient 协议。
        _ = (question, evidence)
        # 正文故意包含危险的绝对承诺，确保测试能证明该草稿不会泄漏到最终响应。
        return GroundedAnswerDraft(
            # 该文本不应出现在图最终返回值中。
            answer="保证立即全额赔付。",
            # 此 ID 不可能来自本次 Qdrant RetrievalHit 候选白名单。
            citation_ids=["fabricated-chunk-id"],
            # 模型自称可回答，但系统不能仅相信这个布尔值。
            is_answerable=True,
        )


class RepeatingToolPlanner:
    """无论观察历史如何都重复同一调用，用于验证循环卡死保护。"""

    async def plan(
        self,
        *,
        user_message: str,
        history: list[ToolExecutionRecord],
    ) -> ToolCallPlan:
        """始终返回同一个工具名和订单号。"""

        # 显式引用输入，说明替身遵循 ToolPlanner 协议但故意忽略观察。
        _ = (user_message, history)
        # 第二轮将产生与第一轮完全相同的工具指纹。
        return ToolCallPlan(
            # 请求调用工具。
            action=AgentAction.CALL_TOOL,
            # 使用白名单内工具，确保精准覆盖重复门而非工具名称白名单。
            tool_name="get_order_status",
            # 固定订单号使第二轮服务端参数指纹重复。
            order_id="SO100001",
            # 简短原因不会进入用户响应。
            reason="故意重复同一个调用以测试安全门。",
        )


class UnauthorizedToolPlanner:
    """建议不存在的写工具，用于验证执行器名称白名单。"""

    async def plan(
        self,
        *,
        user_message: str,
        history: list[ToolExecutionRecord],
    ) -> ToolCallPlan:
        """返回 Schema 合法但业务上不允许的工具名。"""

        # 规划器协议输入不会影响本次越权建议。
        _ = (user_message, history)
        # ToolCallPlan 只约束结构，具体工具权限必须由执行器决定。
        return ToolCallPlan(
            # 结构上仍是调用动作。
            action=AgentAction.CALL_TOOL,
            # 该删除工具不在服务端白名单中，也没有真实实现。
            tool_name="delete_order",
            # 订单号看似合法也不能让未知工具得到执行权限。
            order_id="SO100001",
            # 原因不影响白名单决策。
            reason="尝试调用未授权写工具。",
        )


class FailingOrderRepository:
    """模拟订单数据库或下游服务不可用的仓库。"""

    def get_for_user(self, order_id: str, user_id: str) -> OrderRecord | None:
        """每次查询都抛出固定本地异常。"""

        # 显式引用参数，替身保持与真实 OrderRepository 相同签名。
        _ = (order_id, user_id)
        # 错误正文故意敏感，后续断言确保它不会进入用户答案。
        raise RuntimeError("internal-database-host-is-unavailable")


# 告诉 pytest 在事件循环中执行下面的 async 测试函数。
@pytest.mark.asyncio
async def test_graph_routes_order_request_and_accumulates_events() -> None:
    """验证节点顺序、条件路由和 Reducer 累积事件能够协同工作。"""

    # Act：使用与 API 入口相同的状态结构异步执行整张图，而不是单独调用某个节点。
    result = await service_graph.ainvoke(
        # 这些字段组成测试所需的初始 ServiceState。
        {
            # 使用固定请求标识，让测试结果完全可重复。
            "request_id": "test-request",
            # 使用固定用户标识；当前节点暂不查询真实用户数据。
            "user_id": "user-001",
            # 特意加入首尾和连续空格，并提供属于该用户的示例订单号。
            "user_message": "  我的订单 SO100001   到哪了  ",
            # 预置入口事件，用于验证后续节点事件没有覆盖旧值。
            "events": ["test:started"],
        }
    )

    # Assert：规范化节点应去除首尾空白，并把连续空白折叠为一个空格。
    assert result["normalized_message"] == "我的订单 SO100001 到哪了"
    # Assert：分类节点应识别订单关键词，并写入 ORDER_STATUS 枚举。
    assert result["intent"] == Intent.ORDER_STATUS
    # Assert：当前订单查询是只读路径，因此不需要人工介入。
    assert result["requires_human"] is False
    # Assert：轨迹中必须包含输入规范化事件。
    assert "graph:request_normalized" in result["events"]
    # Assert：轨迹中必须包含订单意图分类事件。
    assert "graph:intent_classified_as_order_status" in result["events"]
    # Assert：轨迹中必须包含工具查询成功事件，证明条件边选择并执行了订单节点。
    assert "graph:order_lookup_succeeded" in result["events"]
    # Assert：订单号被提取并规范化后写入共享状态。
    assert result["order_id"] == "SO100001"
    # Assert：实际工具名被记录，便于 Trace 和面试演示。
    assert result["tool_name"] == "get_order_status"
    # Assert：显式 Agent 循环会记录初始化、两次规划、工具执行和最终汇总，共九项事件。
    assert len(result["events"]) == 9
    # Assert：单订单请求只占用一次真实工具步数。
    assert result["tool_call_count"] == 1
    # Assert：工具观察后规划器选择 finish，最终节点把停止原因改为 completed。
    assert result["agent_stop_reason"] == "completed"


# 第二个集成测试覆盖未知请求的安全兜底路径。
@pytest.mark.asyncio
async def test_graph_routes_unknown_request_to_human() -> None:
    """未知问题必须进入人工路径，不能由系统臆测答案。"""

    # Act：执行一条不包含当前任何已知分类关键词的请求。
    result = await service_graph.ainvoke(
        # 省略 events 也应正常工作，Reducer 会从首个节点事件开始建立列表。
        {
            # 固定请求标识，便于失败时阅读测试输出。
            "request_id": "test-request",
            # 固定用户标识，当前阶段不访问外部用户系统。
            "user_id": "test-user",
            # 该文本故意不包含 FAQ 或订单关键词。
            "user_message": "帮我处理一下这个奇怪的问题",
        }
    )

    # Assert：未知请求必须被显式分类为人工接管，而不是保持 intent 缺失。
    assert result["intent"] == Intent.HUMAN_HANDOFF
    # Assert：最终状态必须明确告诉 API 调用方需要人工处理。
    assert result["requires_human"] is True
    # Assert：只有真正执行人工节点后，轨迹中才会出现该事件。
    assert "graph:human_handoff_requested" in result["events"]


# 第三个集成测试覆盖订单意图正确但缺少工具必需参数的澄清路径。
@pytest.mark.asyncio
async def test_graph_asks_for_order_id_before_calling_tool() -> None:
    """订单问题缺少订单号时应追问，而不是猜测或调用工具。"""

    # Act：输入包含订单关键词，但没有符合格式的订单号。
    result = await service_graph.ainvoke(
        {
            # 固定请求标识便于阅读失败输出。
            "request_id": "test-missing-order-id",
            # 使用种子数据中的合法用户，但本次不会执行查询。
            "user_id": "user-001",
            # 该文本会进入订单路由，却无法通过正则提取工具参数。
            "user_message": "我的订单到哪了",
        }
    )

    # Assert：系统仍正确识别为订单意图。
    assert result["intent"] == Intent.ORDER_STATUS
    # Assert：参数不足时设置澄清标记，而不是错误地转人工。
    assert result["needs_clarification"] is True
    # Assert：没有订单号就不能记录已调用工具。
    assert "tool_name" not in result
    # Assert：轨迹明确记录缺少订单号，便于统计参数完整率。
    assert "graph:order_id_required" in result["events"]


# 第四个集成测试覆盖订单归属安全边界。
@pytest.mark.asyncio
async def test_graph_does_not_reveal_another_users_order() -> None:
    """图通过工具查询其他用户订单时必须返回统一不可用结果。"""

    # Act：user-001 尝试查询实际属于 user-002 的 SO200001。
    result = await service_graph.ainvoke(
        {
            # 固定请求标识便于追踪测试路径。
            "request_id": "test-cross-user-order",
            # 当前可信身份为 user-001。
            "user_id": "user-001",
            # 用户文本中提供另一个用户的有效订单号。
            "user_message": "查询订单 SO200001 的物流",
        }
    )

    # Assert：工具结果必须按不可用处理。
    assert result["tool_result"]["found"] is False
    # Assert：响应不得包含该订单真实的 paid 状态。
    assert "已支付" not in result["answer"]
    # Assert：失败事件不区分“订单不存在”和“订单属于他人”。
    assert "graph:order_lookup_not_available" in result["events"]


# 第五个集成测试覆盖“模型失败 → 安全状态 → 条件边 → 人工响应”的完整路径。
@pytest.mark.asyncio
async def test_graph_routes_model_failure_to_human_without_raising() -> None:
    """外部模型不可用时，整张图应正常结束并生成脱敏人工接管结果。"""

    # Arrange：把无网络故障客户端包装成与生产相同的异步分类节点。
    classifier_node = create_llm_intent_classifier_node(
        # 替身遵循 IntentClassificationClient 协议，但总是在返回前抛出内部错误。
        client=UnavailableClassificationClient(),
        # 阈值与默认生产配置保持一致，但故障路径不会读取该值。
        confidence_threshold=0.65,
    )
    # 使用依赖注入构建独立测试图，不修改进程级 service_graph。
    graph = build_service_graph(classifier_node=classifier_node)

    # Act：执行完整状态图；如果异常边界失效，这里会抛错而不是返回 result。
    result = await graph.ainvoke(
        {
            # 固定请求标识会被降级日志读取。
            "request_id": "test-model-connection-failure",
            # 用户身份仍进入 State，但模型故障后不会调用任何业务工具。
            "user_id": "user-001",
            # 即使文本看起来像订单查询，系统也不能在模型失败后猜测并执行工具。
            "user_message": "查询订单 SO100001 到哪了",
            # 入口事件用于验证 Reducer 仍然完整累积降级轨迹。
            "events": ["test:started"],
        }
    )

    # Assert：最终意图是明确的人工接管安全默认值。
    assert result["intent"] == Intent.HUMAN_HANDOFF
    # Assert：响应准确说明自动服务不可用，而不是声称用户信息不足。
    assert result["answer"] == "自动处理服务暂时不可用，本次请求已建议转交人工客服。"
    # Assert：调用方可以根据该字段创建后续人工任务。
    assert result["requires_human"] is True
    # Assert：内部状态保存有限连接故障码。
    assert result["llm_failure_code"] == "connection"
    # Assert：业务事件先以跨后端统一名称记录最终人工路由结果。
    assert "graph:intent_classified_as_human_handoff" in result["events"]
    # Assert：模型连接故障保留为独立诊断事件，不再污染业务轨迹契约。
    assert "diagnostic:llm_connection_fallback_to_human" in result["events"]
    # Assert：条件边确实继续执行了人工响应节点。
    assert "graph:human_handoff_requested" in result["events"]
    # Assert：模型失败后绝不能执行订单工具。
    assert "tool_name" not in result


# 第六个集成测试覆盖 FAQ 的“分类 → 检索 → 证据门 → grounded answer”完整路径。
@pytest.mark.asyncio
async def test_graph_answers_faq_with_citations() -> None:
    """知识库覆盖的问题应返回证据正文、引用和完整 RAG 轨迹。"""

    # Act：使用默认 mock 分类与本地 Qdrant/hash 索引执行发票问题。
    result = await service_graph.ainvoke(
        {
            # 固定请求标识便于测试失败时阅读轨迹。
            "request_id": "test-faq-with-evidence",
            # 当前公共 FAQ 不按用户区分知识权限，但 State 仍保留身份字段。
            "user_id": "user-001",
            # “发票”触发 FAQ 基线，“税号写错”命中发票政策证据。
            "user_message": "发票税号写错了怎么办",
            # 入口事件用于验证多节点 Reducer 累积。
            "events": ["test:started"],
        }
    )

    # Assert：分类结果保持 FAQ，不会被检索节点改写为其他业务意图。
    assert result["intent"] == Intent.FAQ
    # Assert：最高分证据超过本地基线阈值。
    assert result["retrieval_score"] >= 0.10
    # Assert：回答必须包含发票政策中的“红冲重开”事实。
    assert "红冲重开" in result["answer"]
    # Assert：至少返回一条可追溯引用。
    assert result["citations"]
    # Assert：第一条引用精确指向发票制度。
    assert result["citations"][0].document_id == "KB-INVOICE-001"
    # Assert：轨迹证明先检索证据，再创建 grounded answer。
    assert "graph:faq_evidence_retrieved" in result["events"]
    # Assert：最终回答节点已正常结束。
    assert "graph:faq_grounded_answer_created" in result["events"]


# 第七个集成测试覆盖知识库无充分证据时的安全拒答。
@pytest.mark.asyncio
async def test_graph_routes_faq_without_evidence_to_human() -> None:
    """FAQ 分类正确但知识库无命中时，系统必须转人工而不是使用模型记忆。"""

    # Arrange：构建注入空检索器的独立状态图。
    graph = build_service_graph(
        # 检索替身始终返回空列表，不访问 Qdrant 或外部模型。
        knowledge_retriever=EmptyKnowledgeRetriever(),
        # 固定 Top-K 只用于验证参数传递，不影响空结果。
        faq_top_k=3,
    )

    # Act：输入明确属于 FAQ 的发票问题。
    result = await graph.ainvoke(
        {
            # 固定请求标识便于关联降级轨迹。
            "request_id": "test-faq-no-evidence",
            # 公共 FAQ 路径仍需要完整初始 State。
            "user_id": "user-001",
            # mock 分类器会通过“发票”关键词选择 FAQ。
            "user_message": "发票应该怎么处理",
        }
    )

    # Assert：原始业务意图仍是 FAQ，人工是证据不足后的执行决策。
    assert result["intent"] == Intent.FAQ
    # Assert：无证据必须要求人工介入。
    assert result["requires_human"] is True
    # Assert：拒答文案不能伪造政策答案。
    assert result["answer"] == "知识库中暂未找到足够依据，本次请求已建议转交人工客服。"
    # Assert：无证据不能制造任何引用。
    assert result["citations"] == []
    # Assert：轨迹记录证据门拒绝原因。
    assert "graph:faq_evidence_insufficient" in result["events"]
    # Assert：条件边继续执行了人工节点。
    assert "graph:human_handoff_requested" in result["events"]


# 第八个集成测试覆盖“有证据但生成器伪造引用”的第三道安全门。
@pytest.mark.asyncio
async def test_graph_blocks_fabricated_generation_citation() -> None:
    """模型引用候选证据外 ID 时，草稿必须被丢弃并安全转人工。"""

    # Arrange：检索仍使用真实本地 Qdrant，只替换最终生成客户端。
    graph = build_service_graph(faq_answer_client=FabricatedCitationAnswerClient())

    # Act：该问题能够正常命中发票知识，因此会完整走到生成节点和第三道安全门。
    result = await graph.ainvoke(
        {
            # 固定请求 ID 方便失败时阅读完整事件轨迹。
            "request_id": "test-fabricated-citation",
            # 公共 FAQ 仍携带调用方身份。
            "user_id": "user-001",
            # 发票关键词触发 FAQ，税号问题命中确定性本地知识。
            "user_message": "发票税号写错了怎么办",
            # 验证入口事件不会在安全降级过程中丢失。
            "events": ["test:started"],
        }
    )

    # Assert：模型草稿没有通过候选引用白名单。
    assert result["faq_answer_grounded"] is False
    # Assert：最终用户响应必须来自人工节点，而不是危险模型草稿。
    assert result["answer"] == "知识回答服务暂时不可用，本次请求已建议转交人工客服。"
    # Assert：任何检索预创建引用和伪造引用都被清空。
    assert result["citations"] == []
    # Assert：稳定内部码可以形成“引用越界率”监控指标。
    assert result["rag_failure_code"] == "generation_invalid_citation"
    # Assert：危险承诺没有泄漏到最终用户答案。
    assert "全额赔付" not in result["answer"]
    # Assert：事件证明白名单拦截和人工接管都真正执行。
    assert "graph:faq_generation_invalid_citation_blocked" in result["events"]
    # Assert：第三道条件边把失败状态路由到了人工节点。
    assert "graph:human_handoff_requested" in result["events"]


# 第九个集成测试验证真正的“规划—执行—观察—再规划”多工具循环。
@pytest.mark.asyncio
async def test_order_agent_queries_two_orders_in_explicit_loop() -> None:
    """一次请求包含两个订单时，应执行两次工具并在全部观察后停止。"""

    # Act：两个订单都属于 user-001，默认确定性规划器会按文本顺序逐一处理。
    result = await service_graph.ainvoke(
        {
            # 固定请求标识便于失败时观察事件轨迹。
            "request_id": "test-two-order-agent-loop",
            # 可信系统身份与两个种子订单归属一致。
            "user_id": "user-001",
            # 一次自然语言请求包含两个唯一订单号。
            "user_message": "帮我查询订单 SO100001 和 SO100002 的状态",
            # 入口事件验证 Reducer 跨循环不会覆盖旧事件。
            "events": ["test:started"],
        }
    )

    # Assert：真实工具执行次数正好为两个唯一订单数。
    assert result["tool_call_count"] == 2
    # Assert：订单列表保持用户文本和工具实际执行顺序。
    assert result["queried_order_ids"] == ["SO100001", "SO100002"]
    # Assert：工具节点确实执行两次，而不是一个节点内部批量伪装成循环。
    assert result["events"].count("graph:order_tool_executed") == 2
    # Assert：规划工具动作也出现两次。
    assert result["events"].count("graph:order_agent_planned_tool_call") == 2
    # Assert：两个订单状态都来自各自工具观察。
    assert "SO100001" in result["answer"] and "SO100002" in result["answer"]
    # Assert：所有订单处理完后显式正常停止。
    assert result["agent_stop_reason"] == "completed"
    # Assert：查询成功无需人工。
    assert result["requires_human"] is False


# 第十个集成测试验证错误规划器不会让 LangGraph 无限循环。
@pytest.mark.asyncio
async def test_order_agent_blocks_repeated_tool_call() -> None:
    """规划器忽略观察并重复相同调用时，第二次执行前必须被指纹门拦截。"""

    # Arrange：注入故意卡住的规划器；仓库仍使用真实本地实现。
    graph = build_service_graph(tool_planner=RepeatingToolPlanner())
    # Act：执行一个能够正常查询的订单请求。
    result = await graph.ainvoke(
        {
            # 固定请求标识。
            "request_id": "test-repeated-tool-call",
            # 当前用户拥有目标订单。
            "user_id": "user-001",
            # 文本本身合法，失败仅来自规划器重复动作。
            "user_message": "查询订单 SO100001",
            # 入口事件用于检查完整轨迹。
            "events": ["test:started"],
        }
    )

    # Assert：第一次工具调用成功，重复计划没有形成第二次真实执行。
    assert result["tool_call_count"] == 1
    # Assert：停止原因精确表示重复工具调用。
    assert result["agent_stop_reason"] == "duplicate_tool_call"
    # Assert：内部故障码支持监控重复率。
    assert result["agent_failure_code"] == "duplicate_tool_call"
    # Assert：轨迹证明第二次调用在执行前被阻止。
    assert "graph:order_agent_duplicate_tool_call_blocked" in result["events"]
    # Assert：图正常返回人工结果而不是触发 LangGraph recursion_limit 异常。
    assert result["requires_human"] is True


# 第十一个集成测试验证服务端最大工具步数是硬边界。
@pytest.mark.asyncio
async def test_order_agent_stops_before_exceeding_max_tool_steps() -> None:
    """任务需要更多调用时，第 N+1 个计划必须在执行前停止并转人工。"""

    # Arrange：把独立图的工具预算限制为两次。
    graph = build_service_graph(agent_max_tool_steps=2)
    # Act：问题包含三个唯一合法订单号，因此需要超过两次预算。
    result = await graph.ainvoke(
        {
            # 固定请求标识。
            "request_id": "test-max-tool-steps",
            # 前两个订单属于当前用户，不会因权限提前停止。
            "user_id": "user-001",
            # 第三个订单让规划器在已有两条观察后提出下一次调用。
            "user_message": "查询订单 SO100001、SO100002 和 SO999999",
            # 入口事件用于验证循环轨迹累积。
            "events": ["test:started"],
        }
    )

    # Assert：实际执行严格停在配置上限，没有第三次工具调用。
    assert result["tool_call_count"] == 2
    # Assert：只有前两个订单进入实际查询列表。
    assert result["queried_order_ids"] == ["SO100001", "SO100002"]
    # Assert：停止原因表示工具预算耗尽。
    assert result["agent_stop_reason"] == "max_tool_steps_exceeded"
    # Assert：轨迹证明第三次调用在规划完成后、执行之前被阻止。
    assert "graph:order_agent_max_tool_steps_blocked" in result["events"]
    # Assert：未完成全部任务必须人工接管。
    assert result["requires_human"] is True


# 第十二个集成测试验证模型不能通过结构合法计划调用未授权工具。
@pytest.mark.asyncio
async def test_order_agent_blocks_tool_outside_allowlist() -> None:
    """未知或写操作工具名即使通过计划 Schema，也不能进入实际执行。"""

    # Arrange：注入建议 delete_order 的越权规划器。
    graph = build_service_graph(tool_planner=UnauthorizedToolPlanner())
    # Act：执行普通订单问题。
    result = await graph.ainvoke(
        {
            # 固定请求标识。
            "request_id": "test-tool-allowlist",
            # 合法用户身份不能扩大模型工具权限。
            "user_id": "user-001",
            # 提供合法订单号，确保不是参数问题。
            "user_message": "查询订单 SO100001",
            # 入口事件。
            "events": ["test:started"],
        }
    )

    # Assert：未授权工具没有产生真实调用计数。
    assert result["tool_call_count"] == 0
    # Assert：执行器写入稳定白名单错误码。
    assert result["agent_failure_code"] == "tool_not_allowed"
    # Assert：停止原因与白名单决策一致。
    assert result["agent_stop_reason"] == "tool_not_allowed"
    # Assert：轨迹证明计划到达执行边界后被拒绝。
    assert "graph:order_tool_not_allowed_blocked" in result["events"]
    # Assert：不存在的 delete_order 从未成为公开 tool_name。
    assert result.get("tool_name") is None


# 第十三个集成测试验证订单仓库异常不会击穿 FastAPI 使用的整张图。
@pytest.mark.asyncio
async def test_order_agent_converts_tool_exception_to_safe_handoff() -> None:
    """下游订单服务异常应留下失败观察并返回脱敏人工文案。"""

    # Arrange：只替换仓库，规划、白名单和 LangChain Tool 都使用生产路径。
    graph = build_service_graph(order_repository=FailingOrderRepository())
    # Act：如果执行异常边界失效，ainvoke 会直接抛出 RuntimeError。
    result = await graph.ainvoke(
        {
            # 固定请求标识会进入脱敏日志。
            "request_id": "test-tool-exception",
            # 合法身份允许进入工具边界。
            "user_id": "user-001",
            # 合法订单号保证真实执行到故障仓库。
            "user_message": "查询订单 SO100001",
            # 入口事件。
            "events": ["test:started"],
        }
    )

    # Assert：真实调用尝试占用一次步数。
    assert result["tool_call_count"] == 1
    # Assert：稳定错误码不包含数据库地址或异常正文。
    assert result["agent_failure_code"] == "tool_execution_error"
    # Assert：最终回答使用确定性脱敏文案。
    assert result["answer"] == "订单查询工具暂时不可用，本次请求已建议转交人工客服。"
    # Assert：敏感错误正文没有泄漏到用户响应。
    assert "internal-database-host" not in result["answer"]
    # Assert：失败记录存在但 result 为空，不能成为下一轮模型事实。
    assert result["tool_execution_records"][0].result == {}
    # Assert：图通过人工节点正常结束。
    assert "graph:human_handoff_requested" in result["events"]
