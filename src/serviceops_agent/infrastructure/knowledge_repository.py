"""从本地 JSON 种子文件加载并治理企业知识文档。"""

# Path 负责跨平台文件路径处理，不在代码中拼接 Windows 或 Linux 分隔符。
from pathlib import Path

# Protocol 用于定义可替换知识源；未来可接 CMS、对象存储或文档数据库。
from typing import Protocol

# TypeAdapter 一次性校验整个文档列表，防止部分坏数据静默进入索引。
from pydantic import TypeAdapter

# 知识文档及其发布/访问枚举用于执行索引前治理过滤。
from serviceops_agent.domain.knowledge import (
    KnowledgeAccessScope,
    KnowledgeDocument,
    KnowledgeDocumentStatus,
)


class KnowledgeRepository(Protocol):
    """知识源必须提供的最小读取协议。"""

    def list_indexable_documents(self) -> list[KnowledgeDocument]:
        """返回允许进入公共 FAQ 索引的已发布文档。"""


class JsonKnowledgeRepository:
    """从 UTF-8 JSON 文件读取知识文档的本地仓库实现。"""

    def __init__(self, source_path: Path) -> None:
        """保存知识源路径，真正读取发生在显式方法调用时。"""

        # source_path 通常指向 data/seed/knowledge_documents.json，测试可以注入临时文件。
        self._source_path = source_path

    def list_indexable_documents(self) -> list[KnowledgeDocument]:
        """加载、校验并过滤已发布且允许公共访问的文档。"""

        # read_text 使用明确 UTF-8，确保 Windows 中文政策文本不会依赖系统默认编码。
        raw_json = self._source_path.read_text(encoding="utf-8")
        # validate_json 同时解析 JSON 并校验每个字段类型、长度、枚举和日期格式。
        documents = TypeAdapter(list[KnowledgeDocument]).validate_json(raw_json)
        # 只有已审核发布且公共可见的文档才能成为外部用户回答证据。
        return [
            # 保留原始 Pydantic 对象，后续切片仍能访问完整治理元数据。
            document
            # 遍历经过完整 Schema 校验的知识文档。
            for document in documents
            # DRAFT 和 RETIRED 文档不会进入活动索引。
            if document.status == KnowledgeDocumentStatus.PUBLISHED
            # INTERNAL 文档不会进入公共 FAQ 检索，防止权限边界被向量相似度绕过。
            and document.access_scope == KnowledgeAccessScope.PUBLIC
        ]
