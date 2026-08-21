"""知识治理、切片、本地向量和 Qdrant 检索的无网络单元测试。"""

# Path 指向仓库中的受治理种子知识源。
from pathlib import Path

# MonkeyPatch 用于临时切换工作目录，并在测试结束后自动恢复。
from pytest import MonkeyPatch

# QdrantClient 的内存模式不会创建磁盘文件或访问外部服务。
from qdrant_client import QdrantClient

# PROJECT_ROOT/resolve_project_path 是工作目录无关路径行为的直接测试目标。
from serviceops_agent.config.paths import PROJECT_ROOT, resolve_project_path

# Settings 用于在任意工作目录测试默认相对知识源配置。
from serviceops_agent.config.settings import Settings

# JsonKnowledgeRepository 是发布状态和访问范围过滤的测试目标。
from serviceops_agent.infrastructure.knowledge_repository import JsonKnowledgeRepository

# KnowledgeChunker 负责稳定切片与元数据继承。
from serviceops_agent.rag.chunking import KnowledgeChunker

# HashEmbeddingClient 提供完全确定性、零费用的本地向量。
from serviceops_agent.rag.embeddings import HashEmbeddingClient

# QdrantKnowledgeRetriever 负责建表、写入、Top-K 和阈值过滤。
from serviceops_agent.rag.retriever import (
    QdrantKnowledgeRetriever,
    build_default_knowledge_retriever,
)

# 种子知识源路径相对于 pytest 固定项目根目录。
KNOWLEDGE_SOURCE = Path("data/seed/knowledge_documents.json")


def test_repository_only_returns_published_public_documents() -> None:
    """草稿或内部文档不得进入面向外部用户的向量索引。"""

    # Arrange：创建只读 JSON 知识仓库。
    repository = JsonKnowledgeRepository(KNOWLEDGE_SOURCE)

    # Act：加载允许进入公共 FAQ 索引的文档。
    documents = repository.list_indexable_documents()

    # Assert：种子文件五份文档中只有四份同时满足 published 和 public。
    assert len(documents) == 4
    # Assert：内部补偿规则绝不能出现在公共检索候选中。
    assert all(document.document_id != "KB-INTERNAL-001" for document in documents)
    # Assert：加载结果保留每份政策的可追溯来源。
    assert all(document.source.startswith("kb://serviceops/") for document in documents)


def test_chunker_generates_stable_overlapping_chunks() -> None:
    """相同文档和切片配置应产生相同 ID，并且长文档生成多个窗口。"""

    # Arrange：读取第一份内容较长的已发布退货文档。
    document = JsonKnowledgeRepository(KNOWLEDGE_SOURCE).list_indexable_documents()[0]
    # 使用较小窗口强制单元测试覆盖多切片与 overlap 控制流。
    chunker = KnowledgeChunker(chunk_size=180, chunk_overlap=30)

    # Act：对完全相同文档执行两次切片。
    first_run = chunker.split_document(document)
    # 第二次运行不得依赖随机数或当前时间。
    second_run = chunker.split_document(document)

    # Assert：长政策正文必须被拆为多个可检索证据单元。
    assert len(first_run) >= 2
    # Assert：稳定 UUID 使重复建库能够幂等 upsert，而不是生成重复 Point。
    assert [chunk.chunk_id for chunk in first_run] == [
        chunk.chunk_id for chunk in second_run
    ]
    # Assert：相邻切片起点应早于前一切片终点，证明确实保留了上下文重叠。
    assert first_run[1].start_index < first_run[0].end_index
    # Assert：每个切片都继承父文档版本和来源。
    assert all(chunk.version == document.version for chunk in first_run)


def test_hash_embedding_is_deterministic_and_normalized() -> None:
    """本地基线在不同调用中应返回同维度、同数值的单位向量。"""

    # Arrange：使用较小维度加快测试，生产默认仍为 1024。
    client = HashEmbeddingClient(dimension=128)

    # Act：两次向量化完全相同的中文文本。
    first = client.embed_query("电子发票税号填写错误")
    # 重复调用用于验证哈希算法不依赖 Python 随机种子。
    second = client.embed_query("电子发票税号填写错误")

    # Assert：输出长度必须与 Qdrant Collection 配置一致。
    assert len(first) == 128
    # Assert：相同输入在重复运行中完全一致，适合 CI 基线。
    assert first == second
    # Assert：L2 范数接近 1，使余弦相似度不被文本长度直接支配。
    assert abs(sum(value * value for value in first) - 1.0) < 1e-9


def test_qdrant_retrieves_invoice_evidence_and_rejects_unrelated_query() -> None:
    """本地 RAG 应命中发票制度，并通过阈值拦截明显无关问题。"""

    # Arrange：读取已治理公共文档。
    documents = JsonKnowledgeRepository(KNOWLEDGE_SOURCE).list_indexable_documents()
    # 使用 256 维本地哈希向量，整个测试不访问千问。
    embedding_client = HashEmbeddingClient(dimension=256)
    # 创建独立 Qdrant 内存实例，测试结束后自动释放。
    retriever = QdrantKnowledgeRetriever(
        # :memory: 模式不写入 data/runtime。
        client=QdrantClient(location=":memory:"),
        # 使用测试专属 Collection 名称。
        collection_name="test_knowledge",
        # 文档和查询共享同一个哈希向量空间。
        embedding_client=embedding_client,
        # 0.10 是当前本地基线通过样例校准的最低证据阈值。
        score_threshold=0.10,
    )
    # 建立测试索引；500 字窗口足以保留当前每份短政策的完整上下文。
    retriever.ensure_index(
        documents,
        # 使用与项目默认值相同的切片配置。
        chunker=KnowledgeChunker(chunk_size=500, chunk_overlap=80),
    )

    # Act：查询种子知识库明确覆盖的发票税号问题。
    invoice_hits = retriever.search("发票税号写错了怎么办", top_k=3)
    # 再查询完全不属于售后知识范围的天气问题。
    unrelated_hits = retriever.search("今天天气如何", top_k=3)

    # Assert：至少返回一条达到阈值的发票证据。
    assert invoice_hits
    # Assert：最高分命中必须来自发票政策，而不是其他文档偶然词面碰撞。
    assert invoice_hits[0].chunk.document_id == "KB-INVOICE-001"
    # Assert：明显无关问题全部低于阈值，因此没有可用于回答的证据。
    assert unrelated_hits == []


def test_relative_project_paths_do_not_depend_on_working_directory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """从任意目录启动时，默认知识源仍应解析到仓库 data/seed。"""

    # Act：模拟 PyCharm 把 Working directory 设置为项目外临时目录。
    monkeypatch.chdir(tmp_path)
    # 解析与 .env 默认值相同的相对知识源路径。
    resolved = resolve_project_path("data/seed/knowledge_documents.json")

    # Assert：结果仍然锚定项目根目录，而不是 tmp_path/data/seed。
    assert resolved == (PROJECT_ROOT / "data/seed/knowledge_documents.json").resolve()
    # Assert：解析后的真实知识源文件确实存在。
    assert resolved.is_file()

    # 使用显式离线配置从错误工作目录构建完整默认检索器。
    retriever = build_default_knowledge_retriever(
        Settings(
            # 强制本地向量，测试不会读取用户真实模型配置或产生费用。
            embedding_backend="hash",
            # 内存 Qdrant 不创建临时目录下的运行文件。
            qdrant_location=":memory:",
            # 测试专属 Collection 名称。
            qdrant_collection="test_working_directory_independence",
            # 使用当前本地基线阈值。
            rag_score_threshold=0.10,
        )
    )
    # 查询发票问题，验证不仅路径字符串正确，完整文件加载和索引也成功。
    hits = retriever.search("发票税号写错了怎么办", top_k=1)
    # 最高命中仍应来自发票政策。
    assert hits[0].chunk.document_id == "KB-INVOICE-001"
