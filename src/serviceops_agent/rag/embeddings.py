"""可替换的本地哈希向量与 OpenAI 兼容 Embedding 适配器。"""

# hashlib 提供稳定跨进程哈希；math 用于 L2 归一化；re 用于中英文基础分词。
import hashlib
import math
import re

# Protocol 让向量索引依赖能力接口，而不是某个具体供应商客户端。
from typing import Protocol

# OpenAI 客户端同时支持官方接口和千问的 OpenAI 兼容 Embeddings 端点。
from openai import OpenAI

# Settings 提供后端选择、模型、密钥、Base URL、维度、超时和重试配置。
from serviceops_agent.config.settings import Settings

# 有限错误类别和归一化函数防止具体 SDK 异常泄漏到 RAG 图节点。
from serviceops_agent.llm.errors import (
    LLMFailureKind,
    LLMServiceError,
    normalize_llm_exception,
)


class EmbeddingClient(Protocol):
    """知识索引和查询共同依赖的最小向量化协议。"""

    @property
    def dimension(self) -> int:
        """返回每条向量的固定维度，必须与 Qdrant Collection 一致。"""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化知识切片，并保持输入输出顺序一致。"""

    def embed_query(self, text: str) -> list[float]:
        """向量化一条用户查询。"""


class HashEmbeddingClient:
    """零费用、可重复的本地词元哈希向量基线。

    它用于开发、CI 和故障隔离，不宣称具备真实语义模型的同义表达能力。保留这条基线可以
    用同一检索评测集量化千问 Embedding 相比纯词面方法带来的实际提升。
    """

    def __init__(self, dimension: int) -> None:
        """保存固定向量维度。"""

        # 维度过小会导致大量哈希碰撞，因此复用 Settings 中最小 64 维约束。
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        """返回 Qdrant 建表需要的向量维度。"""

        # 所有文档和查询必须使用同一个维度。
        return self._dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """逐条生成确定性本地向量，不访问网络。"""

        # 列表推导保持与输入 texts 完全相同的顺序。
        return [self._embed_text(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """使用与文档完全相同的哈希空间生成查询向量。"""

        # 查询和文档共享算法才能使用余弦相似度比较。
        return self._embed_text(text)

    def _embed_text(self, text: str) -> list[float]:
        """通过中文单字/双字和英文词元的 signed hashing 生成归一化向量。"""

        # 初始化全零浮点向量，长度由配置固定。
        vector = [0.0] * self._dimension
        # 基础词元同时保留中文局部短语和英文/数字完整单词。
        tokens = self._tokenize(text)
        # 极端空文本使用固定占位词元，避免产生 Qdrant 无法有效比较的全零向量。
        if not tokens:
            # 占位词只用于算法稳定性，不会与正常业务文本大量重合。
            tokens = ["<empty>"]

        # 每个词元稳定映射到一个桶，并用符号位减少单向碰撞偏差。
        for token in tokens:
            # SHA-256 在不同 Python 进程和操作系统中结果一致，适合可重复测试。
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            # 前八字节转换为整数后对维度取模，得到向量桶位置。
            index = int.from_bytes(digest[:8], byteorder="big") % self._dimension
            # 第九字节最低位决定加一还是减一，实现 signed hashing。
            sign = 1.0 if digest[8] & 1 else -1.0
            # 同一词元多次出现会累积词频，重复业务关键词因而获得更高权重。
            vector[index] += sign

        # 计算 L2 范数，使 Qdrant 余弦相似度不受文本长度直接支配。
        norm = math.sqrt(sum(value * value for value in vector))
        # norm 理论上大于零；保护分支避免哈希正负完全抵消时除零。
        if norm == 0.0:
            # 使用第一个固定维度作为最小非零回退向量。
            vector[0] = 1.0
            # 此时向量范数固定为 1。
            return vector
        # 每个维度除以范数，得到单位长度向量。
        return [value / norm for value in vector]

    def _tokenize(self, text: str) -> list[str]:
        """提取英文数字词、中文单字和中文双字词面特征。"""

        # lower 统一英文大小写，不改变中文内容。
        normalized = text.lower()
        # 英文和数字连续串作为完整词元，例如 order、SO100001。
        tokens = re.findall(r"[a-z0-9]+", normalized)
        # 每个连续中文片段分别生成单字与相邻双字特征。
        for block in re.findall(r"[\u4e00-\u9fff]+", normalized):
            # 单字特征提高短查询与政策正文的基本重合召回。
            tokens.extend(block)
            # 双字特征保留“发票”“保修”等中文短语，比纯单字更有区分度。
            tokens.extend(block[index : index + 2] for index in range(len(block) - 1))
        # 返回允许重复的词元列表，重复次数自然表达简单词频。
        return tokens


class OpenAICompatibleEmbeddingClient:
    """调用千问等 OpenAI 兼容 Embeddings 接口的真实向量适配器。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dimension: int,
        batch_size: int,
        timeout_seconds: float,
        max_retries: int,
    ) -> None:
        """创建可复用客户端并保存模型与维度配置。"""

        # OpenAI 客户端只保存于适配器内部，密钥不会进入 LangGraph State。
        self._client = OpenAI(
            # 真实密钥由 SecretStr 显式解包后传入，不写入源码或日志。
            api_key=api_key,
            # 千问使用与聊天模型相同的 OpenAI 兼容基础地址。
            base_url=base_url,
            # 超时限制防止知识检索无限阻塞 API 请求。
            timeout=timeout_seconds,
            # SDK 只处理短暂传输错误；更高层不会盲目重复完整 RAG 流程。
            max_retries=max_retries,
        )
        # 模型名通常为 text-embedding-v4，可通过环境变量替换。
        self._model = model
        # dimension 同时传给 API 并用于校验返回向量长度。
        self._dimension = dimension
        # batch_size来自版本化配置，避免把某一代模型的上限写死在循环中。
        self._batch_size = batch_size
        # api_request_count只累计本适配器真正发出的Embedding HTTP请求，供费用审计。
        self._api_request_count = 0
        # input_token_count累计服务商响应中的输入Token；缺失usage时保持已有值。
        self._input_token_count = 0

    @property
    def dimension(self) -> int:
        """返回真实 Embedding 配置的固定向量维度。"""

        # Qdrant Collection 必须使用相同维度创建。
        return self._dimension

    @property
    def api_request_count(self) -> int:
        """返回当前客户端实际发出的Embedding请求次数。"""

        # 该数字不包含Qdrant本地查询，也不估算SDK内部网络重试。
        return self._api_request_count

    @property
    def input_token_count(self) -> int:
        """返回服务商usage累计的输入Token数量。"""

        # Token由服务商计数，比用中文字符数估算更适合最终成本报告。
        return self._input_token_count

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """按千问当前批次上限分批向量化知识切片。"""

        # 空列表无需发出收费请求，直接返回空结果。
        if not texts:
            # 输出长度与输入保持一致。
            return []
        # vectors 按原始输入顺序累积每批结果。
        vectors: list[list[float]] = []
        # 按当前模型声明的批次大小切分，qwen3.7候选为20，v4应显式配置为10。
        for start in range(0, len(texts), self._batch_size):
            # 当前批次不会超过配置上限。
            batch = texts[start : start + self._batch_size]
            # 复用单批方法完成 SDK 调用、排序与维度校验。
            vectors.extend(self._embed_batch(batch))
        # 返回与输入 texts 一一对应的向量列表。
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """向量化单条用户问题。"""

        # 复用批量端点可以保证查询与文档使用完全相同的模型参数。
        return self._embed_batch([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """执行一次 OpenAI 兼容 Embeddings 请求并验证响应。"""

        try:
            # embeddings.create 对千问会请求兼容端点 /embeddings。
            response = self._client.embeddings.create(
                # 指定配置的向量模型。
                model=self._model,
                # 输入保持列表形式，统一文档和查询调用路径。
                input=texts,
                # 显式指定维度，避免服务商默认值变化导致现有 Collection 不兼容。
                dimensions=self._dimension,
            )
        # 第三方 SDK 异常统一转换成脱敏内部错误，供 FAQ 节点安全转人工。
        except Exception as error:
            # 归一化结果不保存原始服务商响应正文。
            normalized_error = normalize_llm_exception(error)
            # 异常链保留服务端调试能力，但不会进入 State 或 API 响应。
            raise normalized_error from error

        # 请求成功返回后累计一次真实API调用；SDK内部重试无法由业务层精确拆分。
        self._api_request_count += 1
        # OpenAI兼容响应通常提供prompt_tokens；防御性读取兼容缺失usage的供应商。
        usage = getattr(response, "usage", None)
        # 只有存在usage时才读取输入Token。
        if usage is not None:
            # prompt_tokens是Embedding兼容响应中的输入Token字段。
            prompt_tokens = getattr(usage, "prompt_tokens", 0)
            # 某些SDK版本可能返回None，使用or 0保持整数累加。
            self._input_token_count += int(prompt_tokens or 0)

        # SDK data 可能按 index 返回，显式排序保证与请求 texts 顺序一致。
        ordered_items = sorted(response.data, key=lambda item: item.index)
        # 把 SDK 序列转换为普通 list[float]，避免上层依赖具体响应类型。
        vectors = [list(item.embedding) for item in ordered_items]
        # 返回条数不一致说明响应不完整，不能写入错误对应关系的向量。
        if len(vectors) != len(texts):
            # 构造脱敏的格式错误，不包含实际向量或输入文本。
            raise LLMServiceError(LLMFailureKind.INVALID_RESPONSE, retryable=False)
        # 每条向量都必须与 Collection 的固定维度一致。
        if any(len(vector) != self._dimension for vector in vectors):
            # 维度异常通常需要修复模型或配置，当前请求安全失败。
            raise LLMServiceError(LLMFailureKind.INVALID_RESPONSE, retryable=False)
        # 返回已经完成数量、顺序和维度校验的向量。
        return vectors


def create_embedding_client(settings: Settings) -> EmbeddingClient:
    """根据配置创建离线哈希或真实 OpenAI 兼容向量客户端。"""

    # 默认 hash 模式完全离线，适合开发、CI 和面试现场演示。
    if settings.embedding_backend == "hash":
        # 使用与 Qdrant 配置一致的固定维度。
        return HashEmbeddingClient(settings.embedding_dimensions)

    # 真实向量模式复用现有模型密钥；SecretStr 必须显式解包才能取得值。
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    # 缺少密钥时在构建索引前快速失败，避免产生难理解的 401。
    if not api_key:
        # 错误信息只指出配置字段，不包含任何密钥内容。
        raise ValueError("真实 Embedding 必须配置 SERVICEOPS_LLM_API_KEY")
    # OpenAI 兼容向量请求必须使用与密钥地域匹配的 Base URL。
    if not settings.llm_base_url:
        # 在客户端创建前提供清晰配置提示。
        raise ValueError("真实 Embedding 必须配置 SERVICEOPS_LLM_BASE_URL")
    # 创建真实适配器，上层索引仍只依赖 EmbeddingClient 协议。
    return OpenAICompatibleEmbeddingClient(
        # 只把密钥传入 SDK 客户端。
        api_key=api_key,
        # 复用已经验证可调用千问聊天模型的兼容地址。
        base_url=settings.llm_base_url,
        # 使用单独 Embedding 模型配置。
        model=settings.embedding_model,
        # 维度同时约束 API 输出与向量库表结构。
        dimension=settings.embedding_dimensions,
        # 批次大小必须与服务商当前模型限制一致。
        batch_size=settings.embedding_batch_size,
        # 复用模型请求超时时间。
        timeout_seconds=settings.llm_timeout_seconds,
        # 复用有限 SDK 重试次数。
        max_retries=settings.llm_max_retries,
    )
