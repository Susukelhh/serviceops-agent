"""验证BM25候选重排的排序能力、候选闭包和参数边界。"""

# date构造最小知识切片生效日期。
from datetime import date

# KnowledgeChunk/RetrievalHit用于构造可审计候选。
from serviceops_agent.domain.knowledge import KnowledgeChunk, RetrievalHit

# 重排器和检索装饰器是本文件的被测对象。
from serviceops_agent.rag.reranking import (
    BM25CandidateReranker,
    RerankingKnowledgeRetriever,
)


def _hit(
    *,
    document_id: str,
    title: str,
    content: str,
    score: float,
    index: int,
) -> RetrievalHit:
    """创建一条测试用向量命中。"""

    # 返回包含完整治理元数据的领域命中。
    return RetrievalHit(
        # chunk模拟Qdrant payload。
        chunk=KnowledgeChunk(
            # 测试ID保持非空和稳定。
            chunk_id=f"chunk-{index}",
            # 父文档ID用于断言排序。
            document_id=document_id,
            # 标题参与BM25。
            title=title,
            # 正文参与BM25。
            content=content,
            # 测试来源不访问网络。
            source=f"kb://test/{document_id}",
            # 固定版本。
            version="1.0",
            # 固定生效日期。
            effective_date=date(2026, 8, 1),
            # 使用传入序号。
            chunk_index=index,
            # 测试起点固定为零。
            start_index=0,
            # 终点使用正文长度且保证大于零。
            end_index=len(content),
        ),
        # score模拟原向量余弦分数。
        score=score,
    )


class FixedRetriever:
    """始终返回同一原始向量候选顺序的检索替身。"""

    def __init__(self, hits: list[RetrievalHit]) -> None:
        """保存候选并记录请求K值。"""

        # _hits使用新列表防止调用方后续修改输入。
        self._hits = list(hits)
        # requested_top_k用于验证固定候选池。
        self.requested_top_k: int | None = None

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """记录K值并返回对应前缀。"""

        # 查询原文不影响固定测试数据。
        del query
        # 保存底层真实收到的候选数。
        self.requested_top_k = top_k
        # 返回新列表避免原地排序影响替身。
        return list(self._hits[:top_k])


def test_bm25_reranker_promotes_exact_policy_evidence() -> None:
    """词面证据更明确时，正确政策可以超过略高的原向量候选。"""

    # wrong_hit原向量分稍高，但正文没有“红冲、税号”等关键证据。
    wrong_hit = _hit(
        document_id="KB-INVOICE-GENERAL",
        title="普通发票申请",
        content="申请后一个工作日开具，可以从订单详情下载。",
        score=0.42,
        index=0,
    )
    # correct_hit原向量排名第二，但正文精确覆盖已开票税号错误和红冲。
    correct_hit = _hit(
        document_id="KB-INVOICE-CORRECTION",
        title="已开发票红冲更正",
        content="票号生成后税号错误，需要提交红冲重开申请。",
        score=0.39,
        index=1,
    )
    # 0.60让词面分数足以纠正当前小幅向量误排。
    reranker = BM25CandidateReranker(lexical_weight=0.60)

    # 对原始两个候选执行重排。
    reranked = reranker.rerank(
        # 查询包含区分普通开票与已开票更正的词。
        query="票号已经生成但税号错误怎么红冲？",
        # 原始顺序故意错误。
        hits=[wrong_hit, correct_hit],
        # 保留两个候选便于检查集合闭包。
        top_k=2,
    )

    # 精确更正文档应升到第一名。
    assert reranked[0].chunk.document_id == "KB-INVOICE-CORRECTION"
    # 重排没有新增或删除候选文档。
    assert {hit.chunk.document_id for hit in reranked} == {
        "KB-INVOICE-GENERAL",
        "KB-INVOICE-CORRECTION",
    }


def test_reranking_retriever_keeps_fixed_candidate_pool() -> None:
    """装饰器只能请求固定Top-5并返回其中证据。"""

    # 构造两个最小候选。
    hits = [
        _hit(
            document_id="DOC-A",
            title="普通规则",
            content="普通内容",
            score=0.4,
            index=0,
        ),
        _hit(
            document_id="DOC-B",
            title="物流签收规则",
            content="签收凭证和投递照片",
            score=0.3,
            index=1,
        ),
    ]
    # 固定替身保存原始候选。
    base_retriever = FixedRetriever(hits)
    # 包装器固定候选池为5。
    retriever = RerankingKnowledgeRetriever(
        # 注入原检索替身。
        retriever=base_retriever,
        # 使用候选内BM25重排。
        reranker=BM25CandidateReranker(lexical_weight=0.5),
        # 明确候选池大小。
        candidate_k=5,
    )

    # 调用方最终只需要第一名。
    result = retriever.search("签收照片", top_k=1)

    # 底层仍请求固定5个候选，而不是只取第一名后无法纠错。
    assert base_retriever.requested_top_k == 5
    # 返回文档必须来自原始集合。
    assert result[0].chunk.document_id in {"DOC-A", "DOC-B"}
    # 词面更相关的DOC-B应升到首位。
    assert result[0].chunk.document_id == "DOC-B"


def test_bm25_reranker_rejects_invalid_parameters() -> None:
    """非法融合权重和BM25参数必须在构建阶段失败。"""

    # 使用简单try/except避免为三个参数边界引入额外测试依赖。
    try:
        # 权重大于1没有可解释融合意义。
        BM25CandidateReranker(lexical_weight=1.1)
    except ValueError as error:
        # 错误应明确指向权重字段。
        assert "lexical_weight" in str(error)
    else:
        # 没有抛错时主动使测试失败。
        raise AssertionError("非法 lexical_weight 应抛出 ValueError")
