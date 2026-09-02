"""集中管理可以通过环境变量覆盖的项目配置。"""

# lru_cache 用于缓存 Settings 实例，避免每次 API 请求都重新读取环境变量和 `.env`。
from functools import lru_cache

# Literal 将配置值限制在给定字符串集合内，使错误配置在启动阶段就被发现。
from typing import Literal

# Field 为数值配置增加上下界；SecretStr 隐藏密钥；model_validator 校验组合安全规则。
from pydantic import Field, SecretStr, model_validator

# BaseSettings 负责从环境变量读取配置；SettingsConfigDict 配置其读取规则。
from pydantic_settings import BaseSettings, SettingsConfigDict

# PROJECT_ROOT 让 .env 读取不依赖 PyCharm 或命令行的当前工作目录。
from serviceops_agent.config.paths import PROJECT_ROOT

# 该值只为了让首次本地运行无需先配置身份平台；生产环境验证器会明确拒绝它。
DEVELOPMENT_ONLY_JWT_SECRET = (
    "serviceops-development-only-secret-"
    "replace-before-any-real-deployment-2026"
)


class Settings(BaseSettings):
    """应用配置。

    `env_prefix` 让所有配置都使用 `SERVICEOPS_` 前缀，避免与操作系统或其他项目的
    环境变量冲突。`.env` 只用于本地开发，真正的密钥不会提交到 Git。
    """

    # `model_config` 是 Pydantic Settings 的类级配置，不是一个业务配置字段。
    model_config = SettingsConfigDict(
        # 本地开发时自动读取项目根目录的 `.env`；生产环境通常直接注入环境变量。
        env_file=PROJECT_ROOT / ".env",
        # 例如字段 `app_name` 对应 `SERVICEOPS_APP_NAME`，避免与其他应用的变量冲突。
        env_prefix="SERVICEOPS_",
        # `.env` 中出现暂未使用的字段时忽略它，便于分阶段增加配置。
        extra="ignore",
    )

    # 应用显示名称；FastAPI 会把它展示在 Swagger 接口文档的标题中。
    app_name: str = "ServiceOps Agent"
    # 实例标识只用于负载均衡验证和故障定位，不包含主机 IP、用户或其他敏感数据。
    instance_id: str = Field(
        default="local-1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    # 当前运行环境；后续可据此切换日志、调试信息和外部资源配置。
    environment: Literal["development", "test", "production"] = "development"
    # 最低日志级别；低于该级别的日志不会输出，生产环境通常使用 INFO 或 WARNING。
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # telemetry_enabled 统一控制 Trace、Metrics 与关联 JSON 日志；测试会明确关闭它。
    telemetry_enabled: bool = True
    # console 适合本地学习；otlp_http 发送到 Collector；none 保留 API 但不导出数据。
    telemetry_exporter: Literal["none", "console", "otlp_http"] = "console"
    # OpenTelemetry Resource 中的稳定服务名，不能使用每次请求变化的值。
    otel_service_name: str = "serviceops-agent"
    # OTLP/HTTP Collector 根地址；代码会分别追加 /v1/traces 与 /v1/metrics。
    otel_otlp_endpoint: str = "http://127.0.0.1:4318"
    # Trace 采样比例；开发默认全采样，生产应根据流量、成本和风险调整。
    otel_trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    # Metrics 周期导出间隔；过短会增加 Collector 和应用负担。
    otel_metric_export_interval_ms: int = Field(default=60_000, ge=1_000, le=600_000)
    # 影子评测默认关闭；开启后只复用现有终态，不增加任何LLM调用。
    conversation_shadow_enabled: bool = False
    # 稳定哈希采样避免同一请求重试时一会采、一会不采。
    conversation_shadow_sample_rate: float = Field(default=0.10, ge=0.0, le=1.0)
    # 候选身份是部署级有限标签；稳定版和候选版不能共享同一个发布判断窗口。
    conversation_shadow_candidate_id: str = Field(
        default="local-baseline",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9.-]*$",
    )

    # 持久化后端：memory 适合测试；sqlite 适合单机学习；postgres 适合多实例共享数据。
    persistence_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    # LangGraph Checkpointer 数据库路径；相对路径始终以项目根目录解析。
    checkpoint_database_path: str = "data/runtime/checkpoints.sqlite3"
    # 业务退货申请数据库与 Checkpoint 分库，明确区分工作流状态和真实业务记录。
    business_database_path: str = "data/runtime/serviceops.sqlite3"
    # 新会话的默认存活天数；过期后仓库拒绝继续创建轮次，避免无限保存用户历史。
    conversation_ttl_days: int = Field(default=7, ge=1, le=30)
    # 超过该终态轮次数后生成只含结构化槽位的安全摘要，不复制用户原文或模型答案。
    conversation_summary_after_turns: int = Field(default=6, ge=2, le=50)
    # 摘要字符预算同时受领域模型2000字符硬上限保护。
    conversation_summary_max_chars: int = Field(default=1000, ge=200, le=2000)
    # 执行者必须在该窗口内持续续租；超时后恢复器才能进入保守处置。
    lease_duration_seconds: int = Field(default=90, ge=30, le=900)
    # 心跳至少有三次机会在租约到期前成功，降低短暂抖动导致的误判。
    heartbeat_interval_seconds: int = Field(default=20, ge=1, le=300)
    # 到期后额外等待该时间再判为陈旧，吸收数据库与进程调度抖动。
    stale_grace_seconds: int = Field(default=30, ge=0, le=3600)
    # 尚未获得执行租约的accepted轮次超过该时间后才允许恢复器检查。
    accepted_stale_seconds: int = Field(default=60, ge=1, le=3600)
    # PostgreSQL 连接地址同时供业务仓储和 LangGraph Checkpointer 使用；密码不会出现在 repr 中。
    postgres_dsn: SecretStr | None = None
    # 连接池至少保留的连接数；本地默认一条，避免空闲时占用过多数据库资源。
    postgres_pool_min_size: int = Field(default=1, ge=1, le=20)
    # 连接池最多允许的连接数；请求高峰时可临时扩容，但必须设置上限保护数据库。
    postgres_pool_max_size: int = Field(default=5, ge=1, le=100)

    # JWT 对称签名密钥使用 SecretStr，repr/日志不会直接显示原文。
    jwt_secret_key: SecretStr = SecretStr(DEVELOPMENT_ONLY_JWT_SECRET)
    # iss 限制 Token 必须来自预期签发方，防止接受其他系统的同算法 Token。
    jwt_issuer: str = "serviceops-local-issuer"
    # aud 限制 Token 只能用于当前 API，不能把其他服务 Token 横向复用。
    jwt_audience: str = "serviceops-agent-api"
    # 本地演示 Token 默认三十分钟失效，缩小泄漏后的可用窗口。
    jwt_access_token_minutes: int = Field(default=30, ge=1, le=120)
    # 允许少量服务器时钟误差，但不能无限放宽过期校验。
    jwt_clock_skew_seconds: int = Field(default=10, ge=0, le=60)

    # 公网演示开关默认关闭；只有部署者明确开启后，服务才会签发访客短期身份。
    public_demo_enabled: bool = False
    # 访客身份默认十分钟失效，既足够完成四个场景，也缩小链接泄漏后的可用窗口。
    public_demo_token_minutes: int = Field(default=10, ge=1, le=30)
    # 公网输入比内部 API 更短，防止匿名访客用超长文本占满模型上下文和进程内存。
    public_demo_max_message_chars: int = Field(default=500, ge=50, le=1_000)
    # 默认禁止公网匿名流量调用付费模型；部署者理解费用风险后才能显式打开。
    public_demo_allow_paid_model: bool = False

    # 模型后端开关：mock 使用无密钥关键词基线，openai_compatible 调用兼容接口。
    llm_backend: Literal["mock", "openai_compatible"] = "mock"
    # 服务商提供的模型标识；mock 模式不会读取该字段。
    llm_model: str = "replace-with-model-name"
    # 模型服务密钥；SecretStr 的 repr 不会显示明文，None 表示尚未配置。
    llm_api_key: SecretStr | None = None
    # OpenAI 兼容接口的基础地址；不同服务商使用不同地址，因此不在代码中写死。
    llm_base_url: str | None = None
    # 分类任务要求结果稳定，温度默认设为 0；范围限制为常见的 0 到 2。
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    # 单次模型请求最多等待的秒数，避免外部服务异常时无限挂起。
    llm_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    # 模型请求出现暂时性失败时的最大重试次数；过多重试会放大延迟和费用。
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # 模型置信度低于该阈值时强制转人工，防止低把握分类进入自动处理路径。
    intent_confidence_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # 工具规划后端：deterministic 支持零费用回归；llm 使用真实模型逐步选择动作。
    agent_planner_backend: Literal["deterministic", "llm"] = "deterministic"
    # 一次请求最多实际执行的工具次数，防止错误规划器形成无限或高成本循环。
    agent_max_tool_steps: int = Field(default=3, ge=1, le=10)
    # 单个 API 实例允许同时执行的高成本 /api 请求数量；两个实例的总容量约为该值的两倍。
    agent_max_in_flight_requests: int = Field(default=8, ge=1, le=100)
    # 工位已满时最多等待的秒数；短暂排队后仍无容量就快速返回脱敏 503。
    agent_capacity_queue_timeout_seconds: float = Field(
        default=0.05,
        ge=0.001,
        le=5.0,
    )

    # Embedding 后端开关：hash 完全离线；openai_compatible 可调用千问等兼容接口。
    embedding_backend: Literal["hash", "openai_compatible"] = "hash"
    # 真实向量模型名称；第27步候选使用当前千问通用文本模型 qwen3.7-text-embedding。
    embedding_model: str = "qwen3.7-text-embedding"
    # 向量维度必须同时匹配 Embedding 输出和 Qdrant Collection 配置。
    embedding_dimensions: int = Field(default=1024, ge=64, le=2560)
    # 单次 Embedding 请求包含的文本条数；qwen3.7最多20条，v4最多10条。
    embedding_batch_size: int = Field(default=20, ge=1, le=100)

    # Qdrant 的本地存储位置；只有未配置远程 URL 时才使用，保留本地学习和单测能力。
    qdrant_location: str = ":memory:"
    # 独立 Qdrant 服务地址；Docker 中填写 http://qdrant:6333，本地模式保持 None。
    qdrant_url: str | None = None
    # 远程 Qdrant 可选 API Key；SecretStr 防止日志和配置 repr 暴露真实密钥。
    qdrant_api_key: SecretStr | None = None
    # Qdrant 建库、健康检查和查询的单次客户端超时秒数。
    qdrant_timeout_seconds: int = Field(default=10, ge=1, le=120)
    # Collection 名称用于隔离不同知识库、模型版本和向量维度。
    qdrant_collection: str = "serviceops_knowledge_v2"
    # 受治理知识源文件路径；生产环境可通过仓库协议替换为 CMS 或对象存储。
    knowledge_source_path: str = "data/seed/knowledge_documents.json"

    # 每次查询最多返回的候选切片数量，过多会增加上下文噪声和后续生成成本。
    rag_top_k: int = Field(default=3, ge=1, le=10)
    # 余弦相似度低于该阈值视为无充分证据，系统会转人工而不是猜测。
    rag_score_threshold: float = Field(default=0.10, ge=0.0, le=1.0)
    # 查询范围策略在Embedding前拒绝高置信域外和敏感请求；off只用于Baseline对照。
    rag_query_policy: Literal[
        "off",
        "deterministic_v1",
        "deterministic_v2",
    ] = "deterministic_v1"
    # 检索模式：off为纯向量，bm25为旧候选内重排，hybrid_rrf为完整双路召回。
    rag_reranker: Literal["off", "bm25", "hybrid_rrf"] = "hybrid_rrf"
    # BM25词面分数在“向量 + 词面”融合分数中的占比，需要由排序实验选择。
    rag_rerank_lexical_weight: float = Field(default=0.25, ge=0.0, le=1.0)
    # 重排前固定召回的候选切片数，必须不少于最终rag_top_k才有纠错空间。
    rag_rerank_candidate_k: int = Field(default=5, ge=1, le=20)
    # 完整混合召回中，Qdrant 向量通道独立从全库取回的候选数量。
    rag_hybrid_dense_k: int = Field(default=8, ge=1, le=50)
    # 完整混合召回中，BM25 关键词通道独立从全库取回的候选数量。
    rag_hybrid_lexical_k: int = Field(default=8, ge=1, le=50)
    # RRF 排名常数；值越大越看重两路共同出现，越不放大榜首之间的小差异。
    rag_hybrid_rrf_k: int = Field(default=60, ge=1, le=500)
    # 向量榜在 RRF 中的权重，解决同义改写和口语表达的语义召回问题。
    rag_hybrid_dense_weight: float = Field(default=1.0, gt=0.0, le=10.0)
    # BM25 榜在 RRF 中的权重，补强订单术语、政策名称和精确关键词命中。
    rag_hybrid_lexical_weight: float = Field(default=0.8, gt=0.0, le=10.0)
    # 单个知识切片的最大字符数；第一版使用字符近似，后续会增加 Token 精确计数。
    rag_chunk_size: int = Field(default=500, ge=100, le=2000)
    # 相邻切片重复保留的字符数，帮助跨边界问题同时召回前后语义。
    rag_chunk_overlap: int = Field(default=80, ge=0, le=500)
    # FAQ 生成后端：extractive 零费用确定性组织；llm 调用真实聊天模型受约束改写。
    rag_generation_backend: Literal["extractive", "llm"] = "extractive"
    # 生成提示中允许包含的证据总字符数，限制模型上下文、延迟和 Token 成本。
    rag_max_context_chars: int = Field(default=4000, ge=500, le=20_000)

    @model_validator(mode="after")
    def validate_jwt_secret_for_environment(self) -> "Settings":
        """校验数据库连接池、JWT 密钥和生产遥测的组合安全规则。"""

        # 最大连接数不能小于常驻连接数，否则连接池没有可执行的容量范围。
        if self.postgres_pool_max_size < self.postgres_pool_min_size:
            # 报错只显示字段关系，不包含任何连接地址或密码。
            raise ValueError("PostgreSQL 最大连接数不能小于最小连接数")
        if (
            self.heartbeat_interval_seconds * 3
            > self.lease_duration_seconds
        ):
            raise ValueError("执行租约时长必须至少容纳三次心跳间隔")
        # 启用重排时，候选池不能小于最终返回数，否则没有足够候选可供纠错。
        if self.rag_reranker == "bm25" and self.rag_rerank_candidate_k < self.rag_top_k:
            # 错误只描述字段关系，不包含查询或知识内容。
            raise ValueError("RAG 重排候选数不能小于最终 Top-K")
        # 完整混合召回的两条通道都应至少能独立提供最终 Top-K 数量。
        if self.rag_reranker == "hybrid_rrf" and (
            self.rag_hybrid_dense_k < self.rag_top_k
            or self.rag_hybrid_lexical_k < self.rag_top_k
        ):
            # 启动阶段立即暴露组合错误，避免线上某条通道被配置成名存实亡。
            raise ValueError("RAG 混合召回的两路候选数都不能小于最终 Top-K")
        # 只有选择 postgres 模式时才强制连接地址，SQLite 学习模式仍可零配置启动。
        if self.persistence_backend == "postgres" and (
            self.postgres_dsn is None
            or not self.postgres_dsn.get_secret_value().strip()
        ):
            # 启动阶段直接失败，避免请求到达后才发现持久化资源不存在。
            raise ValueError("选择 postgres 持久化后必须配置 SERVICEOPS_POSTGRES_DSN")

        # 只有在校验函数内短暂读取明文，绝不把值拼进错误信息。
        secret_value = self.jwt_secret_key.get_secret_value()
        # HS256 本地密钥至少三十二字符，降低弱密钥风险。
        if len(secret_value) < 32:
            # 错误只说明长度要求，不回显用户输入。
            raise ValueError("JWT 签名密钥至少需要 32 个字符")
        # 生产环境不能使用仓库中的已知开发值或明显占位配置。
        if self.environment == "production" and (
            secret_value == DEVELOPMENT_ONLY_JWT_SECRET
            or secret_value.startswith("replace-")
        ):
            # 启动阶段直接失败，比带默认密钥上线更安全。
            raise ValueError("生产环境必须注入独立的 JWT 签名密钥")
        # production 启用遥测时不能把完整 Span/Metric 直接打印到进程控制台。
        if (
            self.environment == "production"
            and self.telemetry_enabled
            and self.telemetry_exporter == "console"
        ):
            # 生产应显式选择 OTLP Collector 或 none，避免高流量控制台输出。
            raise ValueError("生产环境遥测导出器不能使用 console")
        # 匿名公网入口如果直接连接真实模型，爬虫或恶意请求可能快速消耗账户余额。
        if (
            self.public_demo_enabled
            and self.llm_backend == "openai_compatible"
            and not self.public_demo_allow_paid_model
        ):
            # 必须显式确认成本风险，不能因为复制了本地 .env 就意外开启付费流量。
            raise ValueError("公网演示使用付费模型前必须显式允许付费流量")
        # 返回已经通过组合校验的 Settings。
        return self


# `maxsize=1` 表示进程中只保留一个 Settings 实例，即常见的单例式配置读取方式。
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """创建并缓存配置，避免每次请求都重新读取环境变量和 `.env` 文件。"""

    # 第一次调用会读取环境变量并完成 Pydantic 校验，后续调用直接返回缓存对象。
    return Settings()
