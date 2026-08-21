# 第四步：受治理知识库、Qdrant 检索与有证据回答

## 本步目标

把 FAQ 从占位文本升级为一条完整、可测试的 RAG 安全链路：

```text
已发布公共文档
→ 稳定切片与来源元数据
→ Embedding
→ Qdrant索引
→ 用户查询向量化
→ Top-K + 相似度阈值
→ 有证据回答并返回引用
→ 无证据或检索故障转人工
```

本步最重要的不是“接了一个向量库”，而是建立知识治理、权限过滤、证据门和引用追踪。

## 为什么选择当前技术

- Qdrant Python 客户端支持内存和磁盘本地模式，未来可以替换为 Docker、Kubernetes 或云端
  Qdrant，业务检索协议保持不变。官方说明见
  [Qdrant LangChain/本地模式文档](https://qdrant.tech/documentation/frameworks/langchain/)。
- 千问 `text-embedding-v4` 支持 OpenAI 兼容 `/embeddings`，默认 1024 维，也支持按成本和精度
  选择其他维度。官方说明见
  [阿里云百炼向量化文档](https://help.aliyun.com/zh/model-studio/embedding)。
- 项目默认使用本地 Hash Embedding，保证开发、CI 和面试现场没有网络或额度时仍可运行。

Hash Embedding 只是词面基线，不具备真实语义模型完整的同义表达能力。它的价值是稳定和零成本，
可以作为后续检索评测的对照组，而不是冒充生产语义模型。

## 知识治理发生在向量化之前

`data/seed/knowledge_documents.json` 中每份文档都必须包含：

- 稳定 `document_id`；
- 标题、正文和原始来源；
- 版本和生效日期；
- 发布状态；
- 访问范围。

当前公共 FAQ 索引只接受：

```text
status == published
AND access_scope == public
```

草稿、已废止和内部文档在 Embedding 之前就被过滤。不能先把所有文档写入同一个向量库，再期待
大模型“自觉”不使用内部资料；权限必须由确定性代码和索引隔离控制。

## 切片为什么保留稳定 ID 和原文位置

每个 `KnowledgeChunk` 保存：

- 父文档 ID、标题、版本、生效日期和来源；
- 文档内切片序号；
- 原文起止字符位置；
- 基于文档、版本、位置和内容生成的稳定 UUID。

稳定 UUID 让重复建库可以幂等 upsert；版本或内容变化后会生成新的切片 ID，方便删除旧版本、
审计引用和执行增量索引。

当前切片器优先在段落、换行和中文标点处截断，并保留 overlap。第一版按字符近似长度，后续会
加入模型对应 Tokenizer，避免不同语言下字符数与 Token 数差异过大。

## FAQ 为什么增加第二个条件边

第一条条件边只决定业务意图：

```text
classify_intent → faq / order / human
```

FAQ 还需要第二次决策：

```text
retrieve_faq → answer / human
```

只有 `has_sufficient_evidence=True` 且命中列表非空，才能进入回答节点。状态缺失、低分、
Embedding 失败、Qdrant 失败都会进入人工路径。这是“缺失即拒绝”的 fail-safe 设计。

## 为什么当前先使用确定性证据回答

当前回答节点直接组织已经审核的前两条知识切片，不再次调用 LLM。这一阶段故意把“检索质量”
与“生成质量”分开：

- 检索错误不会被流畅生成掩盖；
- 每句答案都可以直接追溯到切片；
- 测试完全确定，不消耗 Token；
- 可以先建立检索评测集和阈值，再增加受约束生成。

下一阶段可以增加 Grounded Generation：把证据作为唯一上下文交给模型，结构化返回答案和实际
引用 ID；引用 ID 不在候选集合内、答案缺少证据或模型失败时仍然转人工。

## 本地阈值与真实语义阈值不能照搬

当前 Hash Embedding 基线通过少量样例把阈值设置为 `0.10`。它能够命中发票、退货、保修和客服
制度，并过滤天气、写诗等明显无关问题。

切换 `text-embedding-v4` 后，余弦分数分布会变化，不能继续凭经验沿用 `0.10`。正确做法是准备
带相关/不相关标签的检索评测集，根据 Recall@K、MRR、拒答准确率和业务风险选择阈值。

## 在 PyCharm 中运行

右键运行：

```text
examples/04_grounded_faq_rag.py
```

或者在 Terminal 执行：

```powershell
uv run python examples/04_grounded_faq_rag.py
```

重点观察：

1. `retrieval_score` 是否达到阈值；
2. 回答是否只包含知识文档中的内容；
3. Citation 是否包含文档 ID、切片 ID、版本、来源和生效日期；
4. 事件是否依次出现 `faq_evidence_retrieved` 和 `faq_grounded_answer_created`；
5. 天气问题是否返回空检索结果。

该示例强制使用关键词分类器、本地 Hash Embedding 和 Qdrant 内存模式，不消耗千问额度。

## 切换千问 Embedding

在 `.env` 中配置：

```dotenv
SERVICEOPS_EMBEDDING_BACKEND=openai_compatible
SERVICEOPS_EMBEDDING_MODEL=text-embedding-v4
SERVICEOPS_EMBEDDING_DIMENSIONS=1024
SERVICEOPS_QDRANT_LOCATION=data/runtime/qdrant
```

真实向量模式复用 `SERVICEOPS_LLM_API_KEY` 和 `SERVICEOPS_LLM_BASE_URL`。API Key 与 Base URL
必须属于同一地域。首次启动会为知识切片调用 Embedding；持久化 Collection 已存在时不会每次
启动重复向量化。

更换 Embedding 模型或维度时必须使用新的 Collection 名称或显式重建索引，不能把不同向量空间
写进同一个 Collection。

## 本步测试

- 草稿和内部文档不会进入公共索引；
- 切片 ID 稳定、窗口存在 overlap、元数据完整继承；
- Hash Embedding 可重复并完成 L2 归一化；
- Qdrant 正确召回发票政策并过滤无关问题；
- FAQ 完整图返回引用和检索分数；
- 空检索结果经过第二个条件边转人工；
- API 能稳定序列化 Citation；
- 原有模型异常、订单权限和全部接口测试不回归。

## 面试追问

- 为什么权限过滤必须发生在向量化或查询过滤阶段？
- chunk size 和 overlap 如何通过评测选择，而不是拍脑袋？
- 更换 Embedding 模型为什么必须重建索引？
- Top-K、相似度阈值、Recall@K 和上下文噪声之间有什么关系？
- 为什么向量命中不等于答案一定正确？
- 如何处理知识版本、增量更新、删除、回滚和引用审计？
- 本地 Qdrant 与生产集群在持久化、备份、鉴权和高可用上有什么差异？
