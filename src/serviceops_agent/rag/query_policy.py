"""FAQ 检索前的确定性业务范围与敏感请求策略。"""

# re 用于匹配少量高置信度、可审计的域外与敏感请求表达。
import re

# Protocol 描述图节点与评测包装器共同依赖的最小策略能力。
from typing import Literal, Protocol

# BaseModel/Field 让策略结果保持稳定结构和有限原因码。
from pydantic import BaseModel, Field

# KnowledgeRetriever 是被包装的真实检索器协议；RetrievalHit 是search返回类型。
from serviceops_agent.domain.knowledge import RetrievalHit
from serviceops_agent.rag.retriever import KnowledgeRetriever

# QueryPolicyMode 是环境配置和实验Profile允许使用的有限策略版本。
type QueryPolicyMode = Literal["off", "deterministic_v1", "deterministic_v2"]


class KnowledgeQueryAssessment(BaseModel):
    """一次查询进入向量检索前的可解释策略结论。"""

    # allowed 为True时才能继续调用Embedding和Qdrant。
    allowed: bool
    # reason_code 是日志、State事件和实验报告使用的低基数原因码。
    reason_code: str = Field(min_length=1, max_length=100)
    # sensitive 区分普通域外问题与凭据/内部规则探测等安全问题。
    sensitive: bool = False


class KnowledgeQueryPolicy(Protocol):
    """FAQ节点和离线评测共同依赖的查询范围判断协议。"""

    def assess(self, query: str) -> KnowledgeQueryAssessment:
        """判断当前问题是否允许进入企业知识检索。"""


class AllowAllKnowledgeQueryPolicy:
    """保持旧版行为的关闭模式，用于Baseline和回归对照。"""

    def assess(self, query: str) -> KnowledgeQueryAssessment:
        """允许所有非空或空白查询继续由原检索器处理。"""

        # 显式引用query说明本实现遵循同一协议，但关闭模式不读取正文。
        del query
        # 返回稳定允许结果，便于实验区分“关闭策略”与“策略没有命中”。
        return KnowledgeQueryAssessment(
            # 关闭模式不拦截任何请求。
            allowed=True,
            # 原因码说明这是Baseline兼容行为。
            reason_code="policy_off",
            # 关闭模式不对敏感性做判断。
            sensitive=False,
        )


class DeterministicFAQScopePolicy:
    """只拦截高置信度域外或敏感请求的第一版确定性策略。

    该策略不尝试理解所有自然语言，也不替代语义Embedding或LLM意图分类。它只覆盖企业明确
    不提供的天气、投资、医疗、代写，以及内部规则和凭据索取请求；不确定时继续交给检索证据门。
    """

    def __init__(self) -> None:
        """预编译有限规则，避免每次请求重复解析正则表达式。"""

        # sensitive_rules 优先执行，防止敏感探测被普通域外原因覆盖。
        self._sensitive_rules: tuple[tuple[str, re.Pattern[str]], ...] = (
            # 内部补偿、风控、审批额度属于禁止向公共用户披露的组织信息。
            (
                "internal_policy_extraction",
                re.compile(
                    r"(?:内部|机密|vip|高价值客户).{0,12}"
                    r"(?:补偿|赔付|风控|审批|上限|阈值|分级|额度)",
                    # IGNORECASE 让英文VIP大小写不影响结果。
                    re.IGNORECASE,
                ),
            ),
            # 只拦截索取或猜测验证码/密码，不拦截“客服索要验证码是否安全”的安全咨询。
            (
                "credential_extraction",
                re.compile(
                    r"(?:验证码|密码).{0,10}(?:是多少|告诉我|提供|发给我|猜一下)"
                    r"|(?:告诉我|提供|发给我).{0,10}(?:验证码|密码)",
                    # 中文无大小写差异，但统一使用相同编译选项。
                    re.IGNORECASE,
                ),
            ),
        )
        # unsupported_rules 只覆盖高置信度不属于售后FAQ的任务类型。
        self._unsupported_rules: tuple[tuple[str, re.Pattern[str]], ...] = (
            # 天气问题即使带有“退货期”“物流”等词，也不应获得企业政策证据。
            (
                "weather_request",
                re.compile(r"天气|气温|下雨|降雨|晴天|天气预报", re.IGNORECASE),
            ),
            # 股票、基金和投资止损不属于企业售后知识范围。
            (
                "financial_advice_request",
                re.compile(r"股票|基金|证券|止损|投资建议|涨停|跌停", re.IGNORECASE),
            ),
            # 医疗语境中的治疗、手术和身体部位不能套用商品保修政策。
            (
                "medical_request",
                re.compile(
                    r"(?:眼睛|身体|手术|治疗|疾病|医生|药物).{0,14}"
                    r"(?:保修|进液|退款|维修|修复|处理)"
                    r"|(?:保修|进液|退款|维修).{0,14}"
                    r"(?:眼睛|身体|手术|治疗|疾病)",
                    re.IGNORECASE,
                ),
            ),
            # 诗歌、翻译作业和编程请求属于内容生成，不是企业政策问答。
            (
                "content_generation_request",
                re.compile(
                    r"写一首|作诗|现代诗|翻译成|交作业|"
                    r"用\s*python|写.{0,12}(?:程序|代码)",
                    re.IGNORECASE,
                ),
            ),
            # 明确要求申请旧版、试行或废止规则时，不允许用当前文档猜测旧流程。
            (
                "retired_policy_request",
                re.compile(
                    r"(?:以前|旧版|曾经|试行|废止).{0,18}"
                    r"(?:十五天|退货).{0,12}(?:申请|规则|怎么)",
                    re.IGNORECASE,
                ),
            ),
        )

    def assess(self, query: str) -> KnowledgeQueryAssessment:
        """按敏感优先顺序判断问题，并在不确定时安全放行到证据门。"""

        # strip 去掉用户输入两端空白，规则不依赖无意义空格。
        normalized_query = query.strip()
        # 空白问题没有可检索含义，直接拒绝可以避免无意义Embedding调用。
        if not normalized_query:
            # 空白属于普通范围拒绝而不是敏感事件。
            return KnowledgeQueryAssessment(
                # 不进入向量检索。
                allowed=False,
                # 稳定原因码供图节点生成教学事件。
                reason_code="empty_query",
                # 空白查询不携带敏感内容。
                sensitive=False,
            )

        # 先遍历敏感规则，确保凭据和内部政策探测得到更严格原因。
        for reason_code, pattern in self._sensitive_rules:
            # search 允许规则出现在自然语言任意位置。
            if pattern.search(normalized_query):
                # 敏感请求不进入Embedding，避免把敏感探测写入外部模型调用。
                return KnowledgeQueryAssessment(
                    # 拒绝进入知识检索。
                    allowed=False,
                    # 写入当前命中规则的有限原因码。
                    reason_code=reason_code,
                    # 明确标记安全类拒绝。
                    sensitive=True,
                )

        # 再遍历普通域外任务规则。
        for reason_code, pattern in self._unsupported_rules:
            # 任一高置信规则命中即可停止，不需要向量检索证明天气或写诗不相关。
            if pattern.search(normalized_query):
                # 返回普通域外拒绝。
                return KnowledgeQueryAssessment(
                    # 拒绝进入知识检索。
                    allowed=False,
                    # 保存具体域外类型供指标聚合。
                    reason_code=reason_code,
                    # 普通域外问题不是敏感探测。
                    sensitive=False,
                )

        # 没有高置信命中时不做武断拒绝，继续交给Embedding和证据阈值。
        return KnowledgeQueryAssessment(
            # 允许进入下一道证据门。
            allowed=True,
            # 原因码明确表示策略检查完成但没有拦截。
            reason_code="scope_allowed",
            # 当前规则未识别出敏感探测。
            sensitive=False,
        )


class DeterministicFAQScopePolicyV2(DeterministicFAQScopePolicy):
    """在v1高置信规则上，区分“索取凭据”与“咨询凭据安全”。

    v1保留用于复现历史实验；v2只修复一个已经由全新密封集暴露的真实误拒：用户询问客服是否可以
    索要密码或验证码时，应允许系统检索公开安全规则，而不是把用户误判成正在索取凭据本身。
    """

    def __init__(self) -> None:
        """编译安全咨询和明确索密两个互斥优先级规则。"""

        # 初始化v1全部内部政策、凭据、天气、投资、医疗和内容生成规则。
        super().__init__()
        # safety_consultation要求同时出现服务角色、索要/提供语境和凭据词。
        self._credential_safety_consultation = re.compile(
            r"(?:客服|官方|工作人员|维修人员|平台).{0,20}"
            r"(?:索要|要求|让我|让用户|需要我|能否|可以).{0,20}"
            r"(?:密码|验证码)"
            r"|(?:密码|验证码).{0,20}(?:给|提供|告诉).{0,10}"
            r"(?:客服|工作人员|维修人员|官方).{0,10}"
            r"(?:吗|安全|合规|应该|能不能|可不可以)",
            re.IGNORECASE,
        )
        # direct_extraction仍优先拦截“验证码是多少”“直接告诉我”等明确索取内容。
        self._direct_credential_extraction = re.compile(
            r"(?:验证码|密码).{0,10}"
            r"(?:是多少|内容是什么|直接告诉我|发给我|猜一下)"
            r"|(?:直接告诉我|发给我|猜一下).{0,10}(?:验证码|密码)",
            re.IGNORECASE,
        )

    def assess(self, query: str) -> KnowledgeQueryAssessment:
        """安全咨询优先放行，混入明确索密指令时仍交给v1拒绝。"""

        # 与v1一致地忽略两端空白。
        normalized_query = query.strip()
        # 只有明确安全咨询且没有直接索取秘密内容时才应用窄范围例外。
        if (
            self._credential_safety_consultation.search(normalized_query)
            and not self._direct_credential_extraction.search(normalized_query)
        ):
            # 允许进入公开安全知识检索，不把问题标成敏感外发请求。
            return KnowledgeQueryAssessment(
                # 后续证据门仍会决定是否有充分依据。
                allowed=True,
                # 独立原因码便于统计v2修复了多少安全咨询误拒。
                reason_code="scope_allowed_security_consultation",
                # 用户没有提供或索取真实秘密内容。
                sensitive=False,
            )
        # 其他所有请求保持v1行为，避免扩大未知边界。
        return super().assess(normalized_query)


class PolicyFilteredKnowledgeRetriever:
    """为离线评测复用同一查询策略的检索器装饰器。"""

    def __init__(
        self,
        *,
        retriever: KnowledgeRetriever,
        query_policy: KnowledgeQueryPolicy,
    ) -> None:
        """保存真实检索器和前置策略，不修改任何向量索引。"""

        # _retriever 只在策略允许时接收查询。
        self._retriever = retriever
        # _query_policy 与线上FAQ节点使用完全相同的策略实现。
        self._query_policy = query_policy

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """拒绝域外查询，允许的查询继续使用原始Top-K与阈值。"""

        # 每次查询先获得结构化范围结论。
        assessment = self._query_policy.assess(query)
        # 未通过范围门时返回无证据，与检索阈值未通过保持同一最小协议。
        if not assessment.allowed:
            # 空列表会被统一评测为负例正确拒绝或正例错误拒绝。
            return []
        # 允许请求不改变原查询、K值、Embedding或Qdrant排序。
        return self._retriever.search(query, top_k=top_k)


def create_knowledge_query_policy(mode: QueryPolicyMode) -> KnowledgeQueryPolicy:
    """根据稳定配置创建关闭模式或第一版确定性范围策略。"""

    # off 必须保留，保证旧Baseline和A/B实验可以复现。
    if mode == "off":
        # 返回完全放行策略。
        return AllowAllKnowledgeQueryPolicy()
    # v2只增加安全咨询的窄范围放行，不改变其他规则。
    if mode == "deterministic_v2":
        # 返回版本化新策略，旧实验仍可显式使用v1。
        return DeterministicFAQScopePolicyV2()
    # 剩余合法值为deterministic_v1，保持全部历史行为。
    return DeterministicFAQScopePolicy()
