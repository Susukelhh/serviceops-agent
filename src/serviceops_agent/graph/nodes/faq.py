"""FAQ 知识检索、证据阈值判断和带引用回答节点。"""

# logging 记录脱敏检索故障；不会记录用户原文、知识正文或模型密钥。
import logging

# Awaitable/Callable 用于精确标注同步检索节点和异步生成节点签名。
from collections.abc import Awaitable, Callable

# Citation/RetrievalHit 是 FAQ State 中允许保存的强类型证据对象。
from serviceops_agent.domain.knowledge import Citation, RetrievalHit

# ServiceState 统一检索与回答节点的输入输出字段。
from serviceops_agent.graph.state import ServiceState

# LLMServiceError 表示真实 Embedding 调用已经归一化后的脱敏故障。
from serviceops_agent.llm.errors import LLMServiceError

# GroundedAnswerClient 隔离确定性证据组织、真实千问生成和测试替身。
from serviceops_agent.rag.generation import GroundedAnswerClient

# 查询范围策略在调用Embedding前拒绝高置信域外与敏感请求。
from serviceops_agent.rag.query_policy import (
    AllowAllKnowledgeQueryPolicy,
    KnowledgeQueryPolicy,
)

# KnowledgeRetriever 协议允许生产 Qdrant 和测试替身使用同一节点工厂。
from serviceops_agent.rag.retriever import KnowledgeRetriever

# 模块级 Logger 复用应用统一日志配置。
logger = logging.getLogger(__name__)

# FaqNode 统一检索和回答节点的 State 输入与部分状态输出。
type FaqNode = Callable[[ServiceState], dict[str, object]]
# AsyncFaqNode 描述需要等待真实生成模型的异步 FAQ 节点。
type AsyncFaqNode = Callable[[ServiceState], Awaitable[dict[str, object]]]


def create_faq_retrieval_node(
    retriever: KnowledgeRetriever,
    *,
    top_k: int,
    query_policy: KnowledgeQueryPolicy | None = None,
    rerank_event: str | None = None,
) -> FaqNode:
    """创建已经绑定知识检索器和候选数量的 LangGraph 节点。"""

    # 未显式注入时使用旧行为，保证直接调用节点工厂的既有测试保持兼容。
    selected_query_policy = query_policy or AllowAllKnowledgeQueryPolicy()

    def retrieve_faq_evidence(state: ServiceState) -> dict[str, object]:
        """检索 FAQ 证据，并把无结果或外部故障转换成可路由状态。"""

        # 优先使用规范化文本，确保检索与分类看到一致输入。
        query = state.get("normalized_message", "")
        # 请求标识只用于脱敏日志关联，不参与向量检索。
        request_id = state.get("request_id", "unknown")
        # 在任何Embedding或Qdrant调用前执行确定性业务范围判断。
        query_assessment = selected_query_policy.assess(query)
        # 域外或敏感问题不能仅凭业务关键词相似就获得企业政策证据。
        if not query_assessment.allowed:
            # 根据敏感标记选择不包含用户原文的稳定事件前缀。
            event_prefix = (
                # 敏感探测使用security前缀便于单独监控。
                "graph:faq_query_security_rejected"
                # 普通域外请求使用scope前缀。
                if query_assessment.sensitive
                else "graph:faq_query_scope_rejected"
            )
            # 返回与无证据路径兼容的完整状态，但保留更准确的拒绝原因。
            return {
                # 范围门拒绝后没有可回答证据。
                "has_sufficient_evidence": False,
                # 没有调用检索，因此分数明确为零。
                "retrieval_score": 0.0,
                # 清空任何历史命中。
                "retrieval_hits": [],
                # 没有检索就不能生成引用。
                "citations": [],
                # 使用独立故障码区分知识缺口与范围拒绝。
                "rag_failure_code": f"query_{query_assessment.reason_code}",
                # 对外说明只描述职责边界，不回显规则或敏感内容。
                "route_reason": "当前问题不属于可自动检索的企业售后知识范围。",
                # 范围外请求不要求用户通过重复描述来补齐知识。
                "needs_clarification": False,
                # 当前流程安全停止，并交给统一人工/安全响应路径。
                "requires_human": True,
                # 事件只包含低基数原因码，不保存用户原文。
                "events": [f"{event_prefix}:{query_assessment.reason_code}"],
            }

        try:
            # Qdrant 检索器内部完成查询向量化、Top-K 和相似度阈值过滤。
            hits = retriever.search(query, top_k=top_k)
        # 真实千问 Embedding 认证、限流、超时等错误走模型故障分支。
        except LLMServiceError as error:
            # 只记录有限类别和异常类名，不记录服务商响应或查询正文。
            logger.warning(
                "FAQ Embedding 失败并降级到人工接管: request_id=%s kind=%s "
                "retryable=%s",
                request_id,
                error.kind.value,
                error.retryable,
            )
            # 返回完整失败状态，让 FAQ 条件边安全进入人工节点。
            return {
                # False 明确表示没有达到可回答证据门槛。
                "has_sufficient_evidence": False,
                # 0.0 表示本次没有有效检索分数。
                "retrieval_score": 0.0,
                # 没有可靠命中时禁止回答节点读取任何旧证据。
                "retrieval_hits": [],
                # 没有使用证据就不能返回引用。
                "citations": [],
                # 保留有限故障码供人工节点生成准确文案和后续监控聚合。
                "rag_failure_code": f"embedding_{error.kind.value}",
                # 路由原因不暴露服务商、Key 或具体响应内容。
                "route_reason": "知识向量服务暂时不可用，已进入人工接管安全路径。",
                # 系统故障不是用户缺少参数，因此不要求继续补充信息。
                "needs_clarification": False,
                # API 调用方需要创建人工处理任务。
                "requires_human": True,
                # 事件只包含有限错误类别。
                "events": [f"graph:faq_embedding_{error.kind.value}_fallback_to_human"],
            }
        # Qdrant 连接、Collection 或 payload 异常同样不能击穿 FastAPI。
        except Exception as error:
            # 日志只记录 Python 异常类名；具体消息可能包含端点或数据，不直接输出。
            logger.warning(
                "FAQ 向量检索失败并降级到人工接管: request_id=%s cause_type=%s",
                request_id,
                type(error).__name__,
            )
            # 返回与 Embedding 故障相同形状的安全状态，便于条件边统一处理。
            return {
                # 检索失败意味着当前没有充分证据。
                "has_sufficient_evidence": False,
                # 没有可信分数时明确归零。
                "retrieval_score": 0.0,
                # 清空命中，防止使用不完整结果。
                "retrieval_hits": [],
                # 清空引用，保证无证据不引用。
                "citations": [],
                # 稳定内部码用于区分向量库与 Embedding 故障。
                "rag_failure_code": "vector_store_error",
                # 对外保持脱敏、厂商无关说明。
                "route_reason": "企业知识检索服务暂时不可用，已进入人工接管安全路径。",
                # 系统故障不要求用户补参数。
                "needs_clarification": False,
                # 明确请求人工处理。
                "requires_human": True,
                # 事件不包含异常正文。
                "events": ["graph:faq_vector_store_error_fallback_to_human"],
            }

        # Qdrant 已完成阈值过滤，非空列表就表示至少存在一条可用证据。
        has_evidence = bool(hits)
        # 回答最多使用前两条证据，限制上下文噪声并确保每条引用确实被使用。
        selected_hits = hits[:2]
        # 每条 selected_hit 都转换为不包含正文的安全 Citation。
        citations = [Citation.from_hit(hit) for hit in selected_hits]
        # 第一条命中分数最高；无命中时使用 0.0 作为明确默认值。
        top_score = hits[0].score if hits else 0.0

        # 有证据时返回回答节点需要的完整状态。
        if has_evidence:
            # 正常 FAQ 检索不需要人工介入。
            return {
                # 条件边据此进入 grounded answer 节点。
                "has_sufficient_evidence": True,
                # 保存最高分便于阈值评测和面试演示。
                "retrieval_score": top_score,
                # 只保存回答实际使用的前两条命中。
                "retrieval_hits": selected_hits,
                # 引用与 selected_hits 一一对应。
                "citations": citations,
                # 更新路由依据，说明回答来自知识库证据而非模型记忆。
                "route_reason": "企业知识库检索到达到阈值的已发布证据。",
                # 证据充分时无需人工。
                "requires_human": False,
                # FAQ 检索不需要继续向用户收集参数。
                "needs_clarification": False,
                # 事件先标记证据召回，再按实际装配追加有限重排事件。
                "events": ["graph:faq_evidence_retrieved"]
                + ([rerank_event] if rerank_event is not None else []),
            }

        # 无命中不是基础设施错误，而是知识覆盖不足；系统必须拒绝猜测。
        return {
            # 条件边据此进入人工节点。
            "has_sufficient_evidence": False,
            # 没有通过阈值的命中，因此最高可用分数为 0。
            "retrieval_score": 0.0,
            # 明确清空证据列表。
            "retrieval_hits": [],
            # 无证据就不能制造引用。
            "citations": [],
            # 供人工节点选择“知识覆盖不足”而非“用户输入不完整”文案。
            "rag_no_evidence": True,
            # 路由原因清晰表达安全拒答依据。
            "route_reason": "企业知识库没有检索到达到阈值的已发布证据。",
            # 当前自动化无法安全回答，需要人工处理。
            "requires_human": True,
            # 用户重复描述通常不能补齐知识库缺口，因此不设置澄清标记。
            "needs_clarification": False,
            # 事件用于统计知识库覆盖率和拒答率。
            "events": ["graph:faq_evidence_insufficient"],
        }

    # 返回已经捕获 retriever 和 top_k 的同步 LangGraph 节点。
    return retrieve_faq_evidence


def create_faq_answer_node(client: GroundedAnswerClient) -> AsyncFaqNode:
    """创建带引用白名单校验和失败降级的异步 FAQ 生成节点。"""

    async def answer_faq_from_evidence(state: ServiceState) -> dict[str, object]:
        """调用受约束生成器，并确定性验证答案是否真正引用本次候选证据。"""

        # 从 State 读取检索命中；第二道条件边理论上已经保证列表非空。
        raw_hits = state.get("retrieval_hits", [])
        # 只保留运行时类型正确的 RetrievalHit，未知对象不能成为生成证据。
        hits = [hit for hit in raw_hits if isinstance(hit, RetrievalHit)]
        # 防御性检查避免图装配错误或状态污染绕过证据门。
        if not hits:
            # 返回第三道安全门会拒绝的状态，随后进入统一人工节点。
            return {
                # False 表示最终回答尚未通过 grounding 校验。
                "faq_answer_grounded": False,
                # 防止检索阶段的旧引用在失败响应中继续对外显示。
                "citations": [],
                # 设置知识覆盖不足标记，人工节点生成准确拒答文案。
                "rag_no_evidence": True,
                # 记录确定性失败原因。
                "route_reason": "FAQ 生成节点没有收到有效检索证据。",
                # 无证据时必须人工处理。
                "requires_human": True,
                # 事件帮助发现第二道条件边或 State 被错误绕过。
                "events": ["graph:faq_answer_blocked_without_evidence"],
            }

        # 规范化问题与检索阶段使用相同文本，避免生成器看到不同版本输入。
        question = state.get("normalized_message", "")
        # 请求标识只用于脱敏日志关联。
        request_id = state.get("request_id", "unknown")
        try:
            # 生成客户端可能是确定性基线、真实千问模型或单元测试替身。
            draft = await client.generate(question=question, evidence=hits)
        # 真实生成模型的认证、限流、超时和结构化响应错误统一安全降级。
        except LLMServiceError as error:
            # 日志只记录有限故障类别，不记录问题、证据正文或模型响应。
            logger.warning(
                "FAQ Grounded Generation 失败并降级到人工接管: "
                "request_id=%s kind=%s retryable=%s",
                request_id,
                error.kind.value,
                error.retryable,
            )
            # 返回可由第三道条件边继续路由的失败状态，而不是抛出 HTTP 500。
            return {
                # 生成异常意味着答案没有通过 grounding。
                "faq_answer_grounded": False,
                # 失败时不返回检索阶段预创建的引用。
                "citations": [],
                # 有限故障码用于人工文案和监控指标。
                "rag_failure_code": f"generation_{error.kind.value}",
                # 路由原因不包含供应商异常正文。
                "route_reason": "知识回答生成服务暂时不可用，已进入人工接管安全路径。",
                # 系统故障需要人工处理。
                "requires_human": True,
                # 事件包含有限类别，便于聚合但不会泄漏数据。
                "events": [f"graph:faq_generation_{error.kind.value}_fallback_to_human"],
            }

        # 生成器主动判断证据不足时尊重拒答，不强迫模型拼凑答案。
        if not draft.is_answerable:
            # 清空引用并进入人工路径。
            return {
                # 第三道安全门不得放行。
                "faq_answer_grounded": False,
                # 模型拒答时没有被确认实际使用的引用。
                "citations": [],
                # 复用无证据语义，让人工节点返回知识不足说明。
                "rag_no_evidence": True,
                # 清晰记录生成器认为候选证据不足。
                "route_reason": "生成器判断当前候选证据不足以可靠回答。",
                # 当前请求需要人工补充知识或判断。
                "requires_human": True,
                # 单独事件可统计“已召回但生成器拒答”的比例。
                "events": ["graph:faq_generation_declined"],
            }

        # available_hits 是唯一允许引用的白名单，由本次实际 RetrievalHit 构建。
        available_hits = {hit.chunk.chunk_id: hit for hit in hits}
        # 去重同时保持模型返回的引用顺序，避免 API 出现重复 Citation。
        unique_citation_ids = list(dict.fromkeys(draft.citation_ids))
        # 找出任何不属于本次候选集合的编造、过期或越界引用 ID。
        invalid_citation_ids = [
            # 保留非法 ID 只用于布尔判断，不会写入日志或用户响应。
            citation_id
            # 遍历去重后的模型引用。
            for citation_id in unique_citation_ids
            # 候选白名单外的 ID 均视为 grounding 失败。
            if citation_id not in available_hits
        ]
        # 可回答答案必须至少引用一条候选证据，并且不能包含任何越界 ID。
        if not unique_citation_ids or invalid_citation_ids:
            # 清空答案与引用并安全转人工，不能尝试“修正”模型编造的引用。
            return {
                # 引用白名单未通过。
                "faq_answer_grounded": False,
                # 防止检索阶段引用或非法引用对外暴露。
                "citations": [],
                # 稳定内部码便于监控引用越界率。
                "rag_failure_code": "generation_invalid_citation",
                # 不把具体非法 ID 返回给用户。
                "route_reason": "生成答案引用未通过候选证据白名单校验。",
                # 需要人工确认答案和来源。
                "requires_human": True,
                # 事件只说明类别，不包含模型输出内容。
                "events": ["graph:faq_generation_invalid_citation_blocked"],
            }

        # 只根据已经通过白名单的 ID 创建最终 Citation，顺序与答案草稿一致。
        citations = [
            # Citation.from_hit 不会暴露向量、分数或完整内部 payload。
            Citation.from_hit(available_hits[citation_id])
            # 所有 ID 已经确认存在于 available_hits。
            for citation_id in unique_citation_ids
        ]
        # 返回通过三道门的最终 FAQ 成功状态。
        return {
            # draft.answer 已通过 Pydantic 非空和长度校验。
            "answer": draft.answer,
            # True 允许第三道条件边结束本轮图执行。
            "faq_answer_grounded": True,
            # 最终 API 只返回模型实际选择且通过白名单的引用。
            "citations": citations,
            # 成功生成后无需人工。
            "requires_human": False,
            # 路由原因明确引用校验已通过。
            "route_reason": "答案已通过候选证据引用白名单校验。",
            # 记录 grounded answer 成功事件。
            "events": ["graph:faq_grounded_answer_created"],
        }

    # 返回已经捕获具体生成客户端的异步 LangGraph 节点。
    return answer_faq_from_evidence
