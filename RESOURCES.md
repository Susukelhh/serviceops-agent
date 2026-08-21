# AI Agent 应用开发资源

## Knowledge

- [LangGraph 官方文档](https://docs.langchain.com/oss/python/langgraph/overview)
  当前 StateGraph、持久化、中断、流式和可靠执行的权威资料；用于校准课程代码和项目架构。
- [LangChain v1 发布说明](https://docs.langchain.com/oss/python/releases/langchain-v1)
  说明现代 `create_agent`、Middleware 和旧式 Chain/Agent API 的迁移边界；用于避免采用逐渐退出主线的接口。
- [LangChain Models：Structured output](https://docs.langchain.com/oss/python/langchain/models#structured-output)
  第28步通过`with_structured_output(PydanticModel)`把回答、引用ID和可回答决策变成可校验对象。
- [LangChain Retrieval](https://docs.langchain.com/oss/python/langchain/retrieval)
  区分检索器返回相关文档与后续RAG生成有依据答案，支持第28步单变量和第29步端到端分层评测设计。
- [Model Context Protocol 官方 Python SDK](https://py.sdk.modelcontextprotocol.io/)
  MCP Python 客户端、服务端和标准传输的官方资料；只在出现跨系统工具标准化需求时引入。
- [阿里云百炼 Embedding 官方文档](https://help.aliyun.com/en/model-studio/embedding)
  当前可用文本向量模型、维度、批次和地域端点的官方说明；真实候选实验前据此校准配置。
- [Qdrant 官方文档](https://qdrant.tech/documentation/)
  当前向量库 Collection、查询、过滤和本地/服务部署的权威资料。
- [Qdrant Hybrid Search](https://qdrant.tech/documentation/search/text-search/hybrid-search/)
  稠密语义与稀疏关键词候选融合的官方方案；用于第24步后续混合召回实验。
- [Qdrant Hybrid Search with Reranking](https://qdrant.tech/documentation/tutorials-basics/reranking-hybrid-search/)
  先召回候选再重排的官方教程；用于解释 Rerank 只处理已进入候选集的结果。
- [阿里云百炼 Rerank API](https://help.aliyun.com/en/model-studio/api-bailian-2023-12-29-retrieve)
  真实重排模型接口与请求边界；仅在离线候选确认值得付费实验后使用。
- [pgvector 官方仓库](https://github.com/pgvector/pgvector)
  PostgreSQL 向量检索扩展的权威使用资料；用于把业务数据与第一版知识检索统一在 PostgreSQL。
- [OpenTelemetry Python 官方文档](https://opentelemetry.io/docs/languages/python/)
  Trace 与 Metrics 的标准化可观测资料；用于避免把项目绑定在单一观测平台。
- [LangSmith Evaluation Concepts](https://docs.langchain.com/langsmith/evaluation-concepts)
  离线/在线评测、数据集、参考输出、实验和 Agent 工具轨迹评估的官方概念资料。
- [LangGraph Test](https://docs.langchain.com/oss/python/langgraph/test)
  有状态图使用新图与独立 Checkpointer、节点测试和部分执行的官方实践。
- [uv：Using uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)
  使用官方 setup-uv、锁文件、缓存和固定 Python 的一手集成指南。
- [GitHub Actions Secure Use Reference](https://docs.github.com/en/actions/reference/security/secure-use)
  最小权限、完整提交 SHA 固定和第三方 Action 供应链风险的官方指南。
- [GitHub Workflow Artifacts](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflow-artifacts)
  在工作流结束后保存评测报告和失败证据的官方概念资料。
- [GitHub：Removing sensitive data](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
  第30步区分工作区删除、Git历史清理和密钥轮换，避免误以为添加`.gitignore`就能撤回已经提交的秘密。
- [GitHub：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
  说明没有许可证时的默认版权边界；许可证属于项目所有者的法律选择，发布脚本只提醒、不自动添加。
- [阿里云百炼首次调用千问 API](https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen)
  千问模型 ID、API Key 和 OpenAI 兼容调用入口的官方配置资料。
- [阿里云百炼文本向量同步接口](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api/)
  第27步核对 `qwen3.7-text-embedding` 的1024维、单批20条、128K单行上限和免费额度。
- [阿里云百炼模型价格](https://help.aliyun.com/zh/model-studio/model-pricing)
  第27步按服务商返回输入Token和每百万Token 0.5元公开原价计算实验成本。
- [Docker Multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
  用多个构建阶段把依赖安装工具和缓存留在 builder，只向 runtime 复制运行物的官方资料。
- [Docker Build best practices](https://docs.docker.com/build/building/best-practices/)
  基础镜像、缓存、构建上下文、镜像更新与供应链边界的官方指南。
- [FastAPI Docker deployment](https://fastapi.tiangolo.com/deployment/docker/)
  基于官方 Python 自行构建、容器启动顺序和单容器进程模型的当前官方建议。
- [uv Docker integration](https://docs.astral.sh/uv/guides/integration/docker/)
  固定 uv、锁文件同步、分层缓存和项目虚拟环境复制的官方方案。
- [Python Docker Official Image](https://hub.docker.com/_/python)
  当前 Python 3.12 slim Debian 镜像标签与摘要的官方发布入口。
- [SQLite 官方适用场景](https://www.sqlite.org/whentouse.html)
  区分本地应用文件存储与客户端/服务器共享数据库，避免把 SQLite 简单理解为“低级数据库”。
- [PostgreSQL 官方客户端/服务器架构](https://www.postgresql.org/docs/current/tutorial-arch.html)
  说明数据库服务如何管理数据文件、接受多个客户端连接并通过网络与应用协作。
- [LangGraph 官方生产持久化示例](https://docs.langchain.com/oss/python/langgraph/add-memory)
  说明 Checkpointer 如何保存线程状态，以及生产场景使用 PostgreSQL 后端的基本边界。
- [尚硅谷 LangGraph 课程笔记](https://github.com/xbsheng/atguigu-note/tree/main/langgraph)
  用户当前使用的系统课程；用于配合官方文档完成代码练习，不作为 API 最终权威来源。
- [卡码大模型面经](https://notes.kamacoder.com/interview/llm/)
  用作面试问题清单和追问地图；其中示例数字须由项目实验验证，不能当通用结论。

## Wisdom (Communities)

- [LangChain Forum](https://forum.langchain.com/)
  用于核对真实工程问题、迁移经验和生产故障处理方式。
- [LangGraph GitHub Discussions](https://github.com/langchain-ai/langgraph/discussions)
  用于查询框架边界、已知问题和社区实现取舍。

## Gaps

- 项目进入评测阶段后，补充一套高质量的 Agent 安全与离线评测一手资料。
- 正式投递前，持续更新杭州地区的最新岗位样本和毕业时间窗口。
