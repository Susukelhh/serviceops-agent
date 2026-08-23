"""完整混合召回的全语料 BM25、RRF 融合和健康探测测试。"""

# date 构造符合知识领域模型的固定生效日期。
from datetime import date

# pytest 验证非法混合参数会在启动前被拒绝。
import pytest

# Settings 的组合校验保证两条候选池都不小于最终 Top-K。
from serviceops_agent.config.settings import Settings

# KnowledgeChunk 和 RetrievalHit 用真实领域 Schema 约束测试替身。
from serviceops_agent.domain.knowledge import KnowledgeChunk, RetrievalHit

# 本文件直接验证新加入的全语料词面召回和倒数排名融合。
from serviceops_agent.rag.hybrid import BM25CorpusRetriever, ReciprocalRankFusionRetriever


def _chunk(chunk_id: str, title: str, content: str) -> KnowledgeChunk:
    """用固定治理元数据创建最小知识切片，避免每个测试重复字段。"""

    # 所有字段都走生产 Pydantic 校验，测试不会绕过真实数据契约。
    return KnowledgeChunk(
        chunk_id=chunk_id,
        document_id=f"DOC-{chunk_id}",
        title=title,
        content=content,
        source=f"knowledge://{chunk_id}",
        version="1.0",
        effective_date=date(2026, 1, 1),
        chunk_index=0,
        start_index=0,
        end_index=len(content),
    )


class _DenseRetrieverStub:
    """只返回预设向量榜的测试替身，同时记录健康探测是否被委托。"""

    def __init__(self, hits: list[RetrievalHit]) -> None:
        """保存固定命中和初始探测状态。"""

        # 列表副本防止测试外部修改预设顺序。
        self._hits = list(hits)
        # False 表示尚未执行 Qdrant 健康探测。
        self.health_checked = False

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """模拟 Qdrant 只找到固定候选，并遵守调用方数量限制。"""

        # query 在本替身中不参与打分，显式读取以表达它是协议参数。
        _ = query
        # 返回副本防止融合器修改底层预设对象。
        return list(self._hits[:top_k])

    def health_check(self) -> None:
        """记录 RRF 检索器把就绪探测委托给了向量基础设施。"""

        # 测试随后断言该布尔值已变为 True。
        self.health_checked = True


def test_bm25_searches_full_corpus_instead_of_dense_candidate_set() -> None:
    """BM25 必须能召回向量榜完全没有出现的精确术语文档。"""

    # Arrange：发票红冲切片和无关物流切片共同组成完整知识库。
    invoice_chunk = _chunk("invoice", "电子发票红冲", "发票抬头错误时可以申请红冲重开。")
    logistics_chunk = _chunk("logistics", "物流查询", "订单发货后可以查看物流单号。")
    # BM25 在构造阶段拿到全语料，而不是拿到某次 dense_hits。
    lexical_retriever = BM25CorpusRetriever(chunks=[invoice_chunk, logistics_chunk])

    # Act：使用具有区分度的业务术语查询全库。
    hits = lexical_retriever.search("发票红冲怎么办", top_k=2)

    # Assert：精确术语文档位于关键词榜第一。
    assert hits[0].chunk.chunk_id == "invoice"
    # Assert：结果明确标记为 lexical 通道并保留原始 BM25 分数与排名。
    assert hits[0].retrieval_channels == ["lexical"]
    assert hits[0].lexical_score is not None and hits[0].lexical_score > 0.0
    assert hits[0].lexical_rank == 1


def test_rrf_fuses_independent_rankings_and_keeps_channel_evidence() -> None:
    """两路共同命中的证据应优先，同时保留只由某一路发现的候选。"""

    # Arrange：shared 同时会出现在向量榜和关键词榜。
    shared = _chunk("shared", "退货运费", "质量问题退货由商家承担运费。")
    # dense_only 模拟只有语义改写能够找到的内容。
    dense_only = _chunk("dense-only", "售后政策", "符合条件的商品支持售后处理。")
    # lexical_only 包含精确术语，故意不放入向量替身榜。
    lexical_only = _chunk("lexical-only", "运费险规则", "运费险按保单规则理赔。")
    # Qdrant 替身只返回 shared 和 dense_only，证明 BM25 不依赖它的候选集合。
    dense_retriever = _DenseRetrieverStub(
        [
            RetrievalHit(chunk=shared, score=0.91),
            RetrievalHit(chunk=dense_only, score=0.82),
        ]
    )
    # 关键词索引读取整份三切片语料，因此仍有机会找到 lexical_only。
    lexical_retriever = BM25CorpusRetriever(
        chunks=[shared, dense_only, lexical_only]
    )
    # RRF 使用明显但不过度偏向关键词的权重，测试重点是榜单集合与元数据。
    retriever = ReciprocalRankFusionRetriever(
        dense_retriever=dense_retriever,
        lexical_retriever=lexical_retriever,
        dense_k=2,
        lexical_k=3,
        rrf_k=60,
        dense_weight=1.0,
        lexical_weight=1.0,
    )

    # Act：问题同时包含 shared 与 lexical_only 的业务词。
    hits = retriever.search("退货运费和运费险", top_k=3)

    # Assert：两路共同支持的 shared 获得最高 RRF 分数。
    assert hits[0].chunk.chunk_id == "shared"
    assert hits[0].retrieval_channels == ["dense", "lexical"]
    assert hits[0].fusion_method == "rrf"
    # Assert：向量榜没有的 lexical_only 仍由独立 BM25 进入最终候选集合。
    lexical_result = next(hit for hit in hits if hit.chunk.chunk_id == "lexical-only")
    assert lexical_result.dense_rank is None
    assert lexical_result.lexical_rank is not None
    assert lexical_result.retrieval_channels == ["lexical"]


def test_rrf_health_check_delegates_to_qdrant_channel() -> None:
    """应用 readiness 应通过混合检索器真正探测底层 Qdrant。"""

    # Arrange：一条最小语料足以建立 BM25 索引。
    chunk = _chunk("one", "测试知识", "用于验证健康探测委托。")
    dense_retriever = _DenseRetrieverStub([])
    retriever = ReciprocalRankFusionRetriever(
        dense_retriever=dense_retriever,
        lexical_retriever=BM25CorpusRetriever(chunks=[chunk]),
        dense_k=1,
        lexical_k=1,
        rrf_k=60,
        dense_weight=1.0,
        lexical_weight=1.0,
    )

    # Act：执行生产 readiness 会调用的同名方法。
    retriever.health_check()

    # Assert：探测没有被 Python 内存 BM25 假装完成，而是真正下沉到向量路。
    assert dense_retriever.health_checked is True


def test_settings_rejects_hybrid_channel_smaller_than_final_top_k() -> None:
    """任一召回通道小于最终 Top-K 时应在启动阶段拒绝配置。"""

    # Act/Assert：向量路只有 2 条但最终要求 3 条，配置不能通过。
    with pytest.raises(ValueError, match="两路候选数"):
        Settings(
            rag_reranker="hybrid_rrf",
            rag_top_k=3,
            rag_hybrid_dense_k=2,
            rag_hybrid_lexical_k=3,
        )
