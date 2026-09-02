# 第 63 步：Cross-Encoder 二阶段重排候选

## 解决什么问题

RRF 能把向量与 BM25 的独立召回结果合并，但它只利用两路名次，不能联合理解“问题—候选文本”是否
真正相关。本步增加可选 `cross_encoder` 模式：第一阶段仍执行全库 Qdrant + BM25 + RRF，随后把固定
候选池交给 Cross-Encoder 联合评分，最后返回 Top-K。

这里严格保持候选闭包：重排器只能调整已有候选的顺序和分数，不能生成新切片、修改正文或绕过知识
发布范围。输出保留第一阶段 dense/lexical 分数与排名，并把 `fusion_method` 标记为
`cross_encoder`，方便调试器和离线评测区分各层依据。

## 为什么通过独立 TEI 服务调用

[Sentence Transformers 文档](https://www.sbert.net/examples/cross_encoder/applications/README.html)
将 Cross-Encoder 定位为精度更高但更慢的候选重排器；中文候选模型通常达到 GB 级。如果在每个
FastAPI 副本中直接加载模型，会重复占用内存、延长启动时间并让扩容成本与模型大小绑定。因此 API
只实现 [Hugging Face Text Embeddings Inference `/rerank`](https://huggingface.co/docs/text-embeddings-inference/en/http_api)
的有限 HTTP 适配层，模型进程可以独立部署、独立扩缩容。

适配层会校验响应数量、索引唯一性、索引边界以及 0～1 有限分数。TEI 超时、非 2xx、非法 JSON 或
不完整响应统一变成不含远端正文、地址参数和鉴权信息的固定错误；线上工作流随后走现有安全降级，
不会静默伪装成已完成 Cross-Encoder 重排。

## 配置

```dotenv
SERVICEOPS_RAG_RERANKER=cross_encoder
SERVICEOPS_RAG_RERANK_CANDIDATE_K=5
SERVICEOPS_RAG_HYBRID_DENSE_K=8
SERVICEOPS_RAG_HYBRID_LEXICAL_K=8
SERVICEOPS_RAG_CROSS_ENCODER_URL=http://tei-reranker:80
SERVICEOPS_RAG_CROSS_ENCODER_TIMEOUT_SECONDS=5
# SERVICEOPS_RAG_CROSS_ENCODER_API_KEY=仅保存在本机或密钥管理系统
```

候选数必须不小于最终 Top-K，两路第一阶段召回数也必须不小于候选数，错误配置会在启动时失败。
默认仍使用已经离线验证的 `hybrid_rrf`；Cross-Encoder 只有在专门困难集上优于基线、延迟和故障率也
通过门禁后才应晋级，不能仅凭引入模型名称就宣称质量提升。

