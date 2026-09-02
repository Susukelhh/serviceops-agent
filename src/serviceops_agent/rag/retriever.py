"""基于 Qdrant 的知识切片建库与相似度检索。"""

# Protocol 描述 FAQ 节点需要的检索能力，测试可以注入完全内存化替身。
from typing import Protocol

# QdrantClient 同时支持内存、本地磁盘、Docker 服务和云端集群。
from qdrant_client import QdrantClient

# models 提供 Collection 向量配置和批量写入 Point 的强类型结构。
from qdrant_client.http import models

# resolve_project_path 确保相对知识源和索引路径不依赖 PyCharm 当前工作目录。
from serviceops_agent.config.paths import resolve_project_path

# Settings/get_settings 提供索引、切分、Embedding 和检索阈值配置。
from serviceops_agent.config.settings import Settings, get_settings

# 检索领域对象用于校验 Qdrant payload 和向图节点返回稳定结构。
from serviceops_agent.domain.knowledge import KnowledgeDocument, RetrievalHit

# JSON 仓库负责发布状态和访问范围过滤，检索器只接收可索引文档。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# 切片器保证每个向量携带稳定来源、版本和原文位置。
from serviceops_agent.rag.chunking import KnowledgeChunker

# EmbeddingClient 隔离本地哈希与真实千问向量实现。
from serviceops_agent.rag.embeddings import EmbeddingClient, create_embedding_client


class KnowledgeRetriever(Protocol):
    """FAQ LangGraph 节点依赖的最小知识检索协议。"""

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """返回已经按分数降序排列且通过阈值过滤的证据。"""


class HealthCheckableKnowledgeRetriever(KnowledgeRetriever, Protocol):
    """生产默认检索器额外提供独立 Qdrant 的只读就绪探测。"""

    def health_check(self) -> None:
        """确认活动知识 Collection 可访问；失败时直接抛出基础设施异常。"""


class QdrantKnowledgeRetriever:
    """使用 Qdrant 保存知识向量并执行余弦相似度查询。"""

    def __init__(
        self,
        *,
        client: QdrantClient,
        collection_name: str,
        embedding_client: EmbeddingClient,
        score_threshold: float,
    ) -> None:
        """保存可复用的向量库、Collection 和 Embedding 客户端。"""

        # client 可以是内存、本地磁盘或远程 Qdrant，业务代码无需区分。
        self._client = client
        # collection_name 隔离不同知识库或向量模型版本。
        self._collection_name = collection_name
        # embedding_client 确保文档和查询进入同一个向量空间。
        self._embedding_client = embedding_client
        # score_threshold 在向量库查询阶段尽早过滤低证据结果。
        self._score_threshold = score_threshold

    def ensure_index(
        self,
        documents: list[KnowledgeDocument],
        *,
        chunker: KnowledgeChunker,
    ) -> None:
        """幂等创建并补齐 Collection，兼容多个 Agent 实例并发启动。"""

        # 先切片才能知道目标索引应包含多少 Point，并判断共享 Collection 是否已经完整。
        chunks = chunker.split_documents(documents)
        # 没有发布文档时拒绝创建空知识库，防止系统误以为索引已经就绪。
        if not chunks:
            # 错误只描述治理结果，不包含文档正文。
            raise ValueError("没有可写入知识索引的已发布公共文档")
        # exists 保存共享 Qdrant 中是否已经有同名 Collection。
        collection_exists = self._client.collection_exists(self._collection_name)
        # 已存在且 Point 数量不少于当前受治理语料时直接复用，避免重复调用真实 Embedding。
        if collection_exists and self._client.count(
            collection_name=self._collection_name,
            exact=True,
        ).count >= len(chunks):
            # 知识更新仍应通过显式版本化 Collection 发布，本启动逻辑只补齐缺失初始索引。
            return
        # Collection 不存在时先创建空的向量结构；多个副本可能同时走到这里。
        if not collection_exists:
            try:
                # 创建单一默认向量 Collection，距离度量使用余弦相似度。
                self._client.create_collection(
                    # 使用配置中的稳定 Collection 名称。
                    collection_name=self._collection_name,
                    # size 必须与 EmbeddingClient.dimension 完全一致。
                    vectors_config=models.VectorParams(
                        # 固定向量维度。
                        size=self._embedding_client.dimension,
                        # Cosine 分数越高表示查询与知识切片越相似。
                        distance=models.Distance.COSINE,
                    ),
                )
            except Exception:
                # 另一个 Agent 副本抢先创建同名 Collection 属于正常启动竞争。
                if not self._client.collection_exists(self._collection_name):
                    # 若重查仍不存在，说明不是并发冲突，保留原异常阻止假健康启动。
                    raise
        # Collection 缺少完整 Point 时批量生成向量；真实适配器会自动分批请求。
        vectors = self._embedding_client.embed_documents(
            # 标题和正文共同参与向量化，提高短问题召回主题的能力。
            [chunk.embedding_text() for chunk in chunks]
        )
        # 防御性检查避免错误客户端返回数量不一致时把文档与向量错位。
        if len(vectors) != len(chunks):
            # 在写入 Qdrant 前立即终止，避免生成不可审计索引。
            raise ValueError("Embedding 返回数量与知识切片数量不一致")

        # 把每个切片、向量和可过滤元数据组装成 Qdrant Point。
        points = [
            models.PointStruct(
                # Qdrant 接受 UUID 字符串作为稳定 Point ID。
                id=chunk.chunk_id,
                # vector 与同位置 chunk 一一对应。
                vector=vector,
                # mode=json 把 date 转成 ISO 字符串，确保 payload 可序列化。
                payload=chunk.model_dump(mode="json"),
            )
            # strict zip 防止长度不一致被静默截断；上面虽已检查，这里仍保留局部不变量。
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        # wait=True 确保方法返回时索引已经可查询，避免启动后首个请求读不到刚写入数据。
        self._client.upsert(
            # 指定刚创建的 Collection。
            collection_name=self._collection_name,
            # 一次写入当前小型种子知识库的全部 Point。
            points=points,
            # 等待本次更新完成。
            wait=True,
        )

    def health_check(self) -> None:
        """只读获取 Collection 元数据，验证 Qdrant 服务、网络和索引同时可用。"""

        # get_collection 不写数据；远程连接、鉴权或 Collection 异常都会直接抛出。
        self._client.get_collection(collection_name=self._collection_name)

    def search(self, query: str, *, top_k: int) -> list[RetrievalHit]:
        """向量化用户查询，并返回通过证据阈值的领域命中。"""

        # 空白查询不具有检索意义，直接返回无证据结果且不调用真实 Embedding。
        if not query.strip():
            # 空列表会被 LangGraph 路由为人工接管。
            return []
        # 查询必须使用与建库完全相同的 Embedding 客户端和维度。
        query_vector = self._embedding_client.embed_query(query)
        # query_points 在 Qdrant 内执行余弦近邻搜索和阈值过滤。
        response = self._client.query_points(
            # 指定活动知识 Collection。
            collection_name=self._collection_name,
            # 传入当前用户问题向量。
            query=query_vector,
            # 限制返回数量，控制上下文噪声和后续生成成本。
            limit=top_k,
            # 低于阈值的候选不作为回答证据。
            score_threshold=self._score_threshold,
            # 检索命中必须携带完整切片 payload 才能生成引用。
            with_payload=True,
            # 回答阶段不需要再次返回高维向量，减少内存与序列化开销。
            with_vectors=False,
        )
        # hits 保存经过 Pydantic 二次校验的领域结果。
        hits: list[RetrievalHit] = []
        # Qdrant 已按相似度降序返回 ScoredPoint。
        for point in response.points:
            # payload 理论上存在；缺失时跳过，避免把无来源内容作为证据。
            if point.payload is None:
                # 继续检查下一条命中。
                continue
            # RetrievalHit 会递归把 payload 校验为 KnowledgeChunk，并检查 score 范围。
            hit = RetrievalHit.model_validate(
                {
                    # chunk 字段接收 Qdrant 中存储的完整治理元数据。
                    "chunk": point.payload,
                    # point.score 是当前查询与切片的余弦相似度。
                    "score": point.score,
                    # 单独保存原始向量分数，RRF 融合后仍能解释这一通道的贡献。
                    "dense_score": point.score,
                    # Qdrant 返回顺序就是向量榜名次；append 前长度加一得到一基排名。
                    "dense_rank": len(hits) + 1,
                    # 当前命中来自向量通道。
                    "retrieval_channels": ["dense"],
                    # 纯 Qdrant 阶段的分数语义是 dense。
                    "fusion_method": "dense",
                }
            )
            # 追加经过 Schema 校验的命中。
            hits.append(hit)
        # 返回分数降序结果；当前顺序继承自 Qdrant。
        return hits


def create_qdrant_client(
    location: str,
    *,
    url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int = 10,
) -> QdrantClient:
    """优先连接独立 Qdrant 服务，否则创建兼容学习和测试的本地客户端。"""

    # 配置 URL 时不再把索引放进当前 Agent 进程，所有副本共享同一独立服务。
    if url is not None:
        # url 支持 Docker 服务名、内网地址或 Qdrant Cloud HTTPS 地址。
        return QdrantClient(
            url=url,
            api_key=api_key,
            timeout=timeout_seconds,
        )

    # :memory: 是 Qdrant 官方本地内存模式，进程结束后自动释放索引。
    if location == ":memory:":
        # 内存模式适合 CI、学习和不希望写运行文件的场景。
        return QdrantClient(location=":memory:")
    # 其他字符串按本地持久化路径处理，相对值统一锚定到项目根目录。
    storage_path = resolve_project_path(location)
    # Qdrant 会在该目录保存可重用索引，目录已被 .gitignore 排除。
    return QdrantClient(path=str(storage_path))


def build_default_knowledge_retriever(
    settings: Settings | None = None,
) -> HealthCheckableKnowledgeRetriever:
    """从项目配置和受治理 JSON 知识源构建默认检索器。"""

    # 显式 settings 便于测试不同维度和阈值；生产代码读取缓存配置。
    current_settings = settings or get_settings()
    # 仓库只返回 published + public 文档，权限过滤发生在向量化之前。
    repository = JsonKnowledgeRepository(
        # 相对配置路径固定从项目根解析，不受 PyCharm Working directory 影响。
        resolve_project_path(current_settings.knowledge_source_path)
    )
    # 加载并验证所有允许进入公共 FAQ 索引的文档。
    documents = repository.list_indexable_documents()
    # 根据配置选择零费用哈希或真实千问 Embedding。
    embedding_client = create_embedding_client(current_settings)
    # 创建独立服务优先、本地模式后备的 Qdrant 客户端。
    qdrant_client = create_qdrant_client(
        current_settings.qdrant_location,
        url=current_settings.qdrant_url,
        api_key=(
            current_settings.qdrant_api_key.get_secret_value()
            if current_settings.qdrant_api_key is not None
            else None
        ),
        timeout_seconds=current_settings.qdrant_timeout_seconds,
    )
    # 组装 Qdrant 检索器，但此时尚未必存在 Collection。
    retriever = QdrantKnowledgeRetriever(
        # 注入已创建的 Qdrant 客户端。
        client=qdrant_client,
        # 使用配置的 Collection 名称。
        collection_name=current_settings.qdrant_collection,
        # 文档与查询共享同一个向量客户端。
        embedding_client=embedding_client,
        # 使用配置的证据最低分数。
        score_threshold=current_settings.rag_score_threshold,
    )
    # 切片器使用配置窗口和重叠，保证索引构建参数可审计。
    chunker = KnowledgeChunker(
        # 单切片最大字符数。
        chunk_size=current_settings.rag_chunk_size,
        # 相邻切片重复字符数。
        chunk_overlap=current_settings.rag_chunk_overlap,
    )
    # 内存模式每次启动创建索引；持久化 Collection 存在时不会重复向量化。
    retriever.ensure_index(documents, chunker=chunker)
    # 关闭重排时返回原Qdrant顺序，用于冻结Baseline和历史实验复现。
    if current_settings.rag_reranker == "off":
        # 原检索器已经可以直接处理search。
        return retriever
    # 完整混合模式和 Cross-Encoder 都先独立召回向量与词面候选。
    first_stage_retriever: HealthCheckableKnowledgeRetriever = retriever
    if current_settings.rag_reranker in {"hybrid_rrf", "cross_encoder"}:
        # 局部导入避免 hybrid 模块引用检索协议时形成模块初始化循环。
        from serviceops_agent.rag.hybrid import (  # noqa: PLC0415
            BM25CorpusRetriever,
            ReciprocalRankFusionRetriever,
        )

        # BM25 使用与 Qdrant 完全相同的治理后切片，但独立建立全语料词面索引。
        lexical_retriever = BM25CorpusRetriever(
            chunks=chunker.split_documents(documents),
        )
        # 第一阶段保留每路原分数、排名和最终 RRF 分数。
        first_stage_retriever = ReciprocalRankFusionRetriever(
            dense_retriever=retriever,
            lexical_retriever=lexical_retriever,
            dense_k=current_settings.rag_hybrid_dense_k,
            lexical_k=current_settings.rag_hybrid_lexical_k,
            rrf_k=current_settings.rag_hybrid_rrf_k,
            dense_weight=current_settings.rag_hybrid_dense_weight,
            lexical_weight=current_settings.rag_hybrid_lexical_weight,
        )
        # 纯混合模式直接返回；Cross-Encoder 会继续联合编码固定候选池。
        if current_settings.rag_reranker == "hybrid_rrf":
            return first_stage_retriever
    # 局部导入避免reranking模块为类型协议反向导入本模块时形成初始化循环。
    from serviceops_agent.rag.reranking import (  # noqa: PLC0415
        BM25CandidateReranker,
        CrossEncoderCandidateReranker,
        RerankingKnowledgeRetriever,
        TEICrossEncoderScoringClient,
    )

    if current_settings.rag_reranker == "cross_encoder":
        # TEI 在独立进程加载模型，API 进程只发送固定候选文本并校验有限响应。
        return RerankingKnowledgeRetriever(
            retriever=first_stage_retriever,
            reranker=CrossEncoderCandidateReranker(
                scoring_client=TEICrossEncoderScoringClient(
                    base_url=current_settings.rag_cross_encoder_url or "",
                    api_key=(
                        current_settings.rag_cross_encoder_api_key.get_secret_value()
                        if current_settings.rag_cross_encoder_api_key is not None
                        else None
                    ),
                    timeout_seconds=current_settings.rag_cross_encoder_timeout_seconds,
                )
            ),
            candidate_k=current_settings.rag_rerank_candidate_k,
        )

    # 返回第26步晋级的候选内重排装饰器。
    return RerankingKnowledgeRetriever(
        # 原Qdrant检索器负责召回和阈值过滤。
        retriever=first_stage_retriever,
        # BM25只占冻结权重，不覆盖全部向量语义分数。
        reranker=BM25CandidateReranker(
            lexical_weight=current_settings.rag_rerank_lexical_weight
        ),
        # 固定候选池必须不小于最终Top-K，Settings已完成组合校验。
        candidate_k=current_settings.rag_rerank_candidate_k,
    )
