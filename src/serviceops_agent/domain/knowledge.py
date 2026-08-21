"""知识库文档、切片、检索命中和引用的稳定领域模型。"""

# date 用于校验政策生效日期；StrEnum 为状态和访问范围提供有限字符串集合。
from datetime import date
from enum import StrEnum

# BaseModel 提供运行时校验；Field 为知识字段声明长度、数值范围和接口说明。
from pydantic import BaseModel, Field


class KnowledgeDocumentStatus(StrEnum):
    """企业知识文档的发布生命周期。"""

    # DRAFT 表示仍在编辑，不能进入面向用户的检索索引。
    DRAFT = "draft"
    # PUBLISHED 表示已经审核发布，可以成为回答证据。
    PUBLISHED = "published"
    # RETIRED 表示已失效或被新版本替代，必须从活动索引排除。
    RETIRED = "retired"


class KnowledgeAccessScope(StrEnum):
    """知识文档允许服务的访问范围。"""

    # PUBLIC 表示可以用于所有已通过 API 边界的用户请求。
    PUBLIC = "public"
    # INTERNAL 表示只允许内部员工或特定角色访问，本阶段不会进入公共 FAQ 索引。
    INTERNAL = "internal"


class KnowledgeDocument(BaseModel):
    """从受治理知识源加载的一份完整企业文档。"""

    # document_id 是跨版本保持稳定的业务标识，不依赖文件路径或标题。
    document_id: str = Field(min_length=1, max_length=100)
    # title 是引用中展示给用户的可读名称，也会参与向量化以增强主题召回。
    title: str = Field(min_length=1, max_length=200)
    # content 保存经过审核的正文，切片器不会修改原始知识源文件。
    content: str = Field(min_length=1, max_length=50_000)
    # source 是可追溯来源标识，可以是企业文档 URL、知识库路径或制度编号。
    source: str = Field(min_length=1, max_length=500)
    # version 使引用和索引能够区分同一制度的不同发布版本。
    version: str = Field(min_length=1, max_length=50)
    # effective_date 表示当前政策从哪一天开始生效，避免回答使用时间不明确的规则。
    effective_date: date
    # status 决定文档是否已经通过发布流程，只有 published 才能进入活动索引。
    status: KnowledgeDocumentStatus
    # access_scope 防止内部知识误进入公共 FAQ 回答。
    access_scope: KnowledgeAccessScope


class KnowledgeChunk(BaseModel):
    """由一份已发布文档切分得到的可检索最小证据单元。"""

    # chunk_id 使用稳定 UUID 字符串，重复建库时同一切片保持相同 Qdrant Point ID。
    chunk_id: str = Field(min_length=1, max_length=100)
    # document_id 关联回完整知识文档，便于版本更新和批量删除旧切片。
    document_id: str = Field(min_length=1, max_length=100)
    # title 从父文档复制，用于向量化和用户引用展示。
    title: str = Field(min_length=1, max_length=200)
    # content 是实际送入检索和回答节点的证据文本。
    content: str = Field(min_length=1, max_length=5000)
    # source 从父文档继承，确保每个独立命中都能追溯来源。
    source: str = Field(min_length=1, max_length=500)
    # version 从父文档继承，避免引用只显示标题却无法定位具体版本。
    version: str = Field(min_length=1, max_length=50)
    # effective_date 从父文档继承，供回答和审计判断政策时效性。
    effective_date: date
    # chunk_index 是该切片在父文档中的零基序号，用于稳定排序和调试切分效果。
    chunk_index: int = Field(ge=0)
    # start_index 表示切片在原始正文中的起始字符位置，便于回溯原文。
    start_index: int = Field(ge=0)
    # end_index 表示切片在原始正文中的结束字符位置，必须大于 start_index。
    end_index: int = Field(gt=0)

    def embedding_text(self) -> str:
        """组合标题和正文作为向量输入，提高短查询对文档主题的命中率。"""

        # 标题与正文使用换行分隔，既保留主题词也不改变正文内容。
        return f"{self.title}\n{self.content}"


class RetrievalHit(BaseModel):
    """向量检索返回的一条经过领域校验的命中结果。"""

    # chunk 是完整证据切片，回答节点只能使用这里存在的知识内容。
    chunk: KnowledgeChunk
    # score 是 Qdrant 返回的余弦相似度，用于阈值判断和排序解释。
    score: float = Field(ge=-1.01, le=1.01)


class Citation(BaseModel):
    """可以安全暴露给 API 调用方的知识来源引用。"""

    # document_id 让前端或审计系统定位稳定业务文档。
    document_id: str
    # chunk_id 精确定位本次答案实际使用的证据切片。
    chunk_id: str
    # title 是展示给用户的可读引用标题。
    title: str
    # source 指向原始企业知识来源，而不是向量数据库内部 ID。
    source: str
    # version 标明回答依据的具体制度版本。
    version: str
    # effective_date 标明该制度的生效时间。
    effective_date: date

    @classmethod
    def from_hit(cls, hit: RetrievalHit) -> "Citation":
        """从检索命中提取引用字段，不把整段知识正文暴露为元数据。"""

        # 使用局部变量减少后续字段访问重复，并明确所有信息来自已校验切片。
        chunk = hit.chunk
        # 构造新的 Citation，避免 API 意外序列化 RetrievalHit 中的 score 和全文。
        return cls(
            # 复制稳定父文档标识。
            document_id=chunk.document_id,
            # 复制本次实际命中的切片标识。
            chunk_id=chunk.chunk_id,
            # 复制用户可读标题。
            title=chunk.title,
            # 复制可追溯原始来源。
            source=chunk.source,
            # 复制政策版本。
            version=chunk.version,
            # 复制政策生效日期。
            effective_date=chunk.effective_date,
        )


class GroundedAnswerDraft(BaseModel):
    """FAQ 生成器必须返回、但尚未通过引用白名单校验的结构化草稿。"""

    # answer 是面向用户的简洁答案；最终节点仍会检查可回答标记和引用是否合法。
    answer: str = Field(min_length=1, max_length=2000)
    # citation_ids 只能引用提示中给出的 chunk_id，节点会执行确定性集合校验。
    citation_ids: list[str] = Field(default_factory=list, max_length=5)
    # is_answerable 表示生成器认为当前证据是否足以回答，而不是对自身知识的主观把握。
    is_answerable: bool
