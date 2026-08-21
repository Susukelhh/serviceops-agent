# 第29步：端到端RAG组合晋级门

## 为什么局部实验通过还不够

第25～28步分别验证了范围门、候选排序、语义Embedding和证据充分性，但真实请求会连续经过所有模块。
局部模块都能运行，不代表组合后仍满足答案长度、调用次数、引用和拒答约束。本步因此建立“总装验收”：

```text
用户问题
  → 确定性范围门
  → Embedding + Qdrant Top-5
  → 25% BM25候选内重排
  → Grounded is_answerable判断
  → 引用白名单与正确来源校验
  → 自动回答或安全转人工
```

## 两套完整Profile

| Profile | 范围门 | Embedding | 阈值 | 重排 | 回答器 |
|---|---|---|---:|---|---|
| 当前离线基线 | deterministic_v1 | Hash 1024维 | 0.10 | BM25 25% | Extractive |
| 真实组合候选 | deterministic_v1 | qwen3.7-text-embedding 1024维 | 0.50 | BM25 25% | qwen-plus Grounded |

这不是用一个实验同时证明两个组件的因果效果。第27、28步已经完成单变量诊断；本步只回答一个新的工程问题：
“接受的组件组合到一起以后，完整用户请求能否通过质量门？”

## 数据与holdout纪律

- 开发集16条：11条可回答、5条知识缺口；包含第27、28步已经揭晓的Bad Case；
- 锁定集12条：8条可回答、4条知识缺口；只有候选开发门通过且完整指纹冻结后才能读取；
- 候选指纹同时覆盖语料版本、切片、Embedding模型与维度、阈值、Top-K、BM25权重和Grounded系统提示；
- 范围门拦截的问题不会发送给外部Embedding或聊天模型；
- 查询先批量Embedding并缓存，逐题评测不会产生隐藏请求。

## 七项质量指标

| 指标 | 回答的问题 |
|---|---|
| Retrieval Recall | 正确文档有没有进入最终Top-K？ |
| Top-1 Accuracy | 重排后的第一份文档是否正确？ |
| Answerable Recall | 有答案时，最终是否回答并引用正确来源？ |
| Abstention Accuracy | 知识缺口最终是否拒答？ |
| Decision Accuracy | 全部最终回答/拒答决策有多少正确？ |
| Unsupported Answer Rate | 知识缺口中仍被错误自动回答的比例？ |
| Citation Validity | 放行答案是否只引用本轮候选白名单？ |

## 分层失败归因

报告不会只写“答案错误”，而会保存请求的公开终止阶段和有限原因码：

- `scope_false_rejection`：范围门误拒了本来可回答的问题；
- `retrieval_miss`：范围允许，但Top-K没有正确文档；
- `grounding_declined_answerable_case`：正确证据已召回，回答器仍拒答；
- `unsupported_answer_generated`：知识缺口被错误自动回答；
- `invalid_or_missing_citation`：放行答案无引用或引用越界；
- `answer_did_not_cite_expected_document`：引用合法，但没有引用人工标注的正确父文档。

这些是工程执行轨迹，不是模型隐藏思维过程。

## 离线总装结果

默认命令没有读取Key，也没有运行真实候选或锁定集：

| Profile | 检索Recall | Top-1 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Hash + BM25 + Extractive | 100% | 90.91% | 100% | 60% | 87.5% | 40% | 100% | FAIL |

两个失败样本正是已知回归题：支付方式问题误命中数字商品规则，上门取件问题误命中物流/运费/退货规则。
这说明范围门能拦截天气和内部制度，却不能穷举所有“看起来像售后”的知识缺口；回答层仍然必要。

总装测试还发现旧Extractive在Top-5长证据下会超过`GroundedAnswerDraft.answer`的2000字符Schema上限。
现在它会按字符预算依次装入证据，并且只引用真正写入答案的Chunk。该问题在模块隔离测试中没有出现，正是
端到端验收的价值。

## PyCharm运行配置

先建立完全离线配置：

- Name：`29 RAG end-to-end baseline`
- Script path：`D:\serviceops-agent\examples\29_rag_end_to_end_experiment.py`
- Parameters：留空
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- Environment variables：`PYTHONUTF8=1`

真实开发候选复制该配置并改名为`29 Qwen RAG end-to-end development`，Parameters填写：

```text
--confirm-paid-api
```

计划约3次Embedding业务请求；最多14次聊天调用。真实空检索会减少聊天调用。第一次运行后检查Bad Case和
质量门；即使开发门通过，也会因为`frozen_candidate_fingerprint`尚未填写而以退出码1结束，这是预期保护。

只有开发候选通过后，才能把脚本打印的64位候选指纹复制到
`data/evaluation/rag_end_to_end_experiment.json`的`frozen_candidate_fingerprint`。复测开发指纹匹配后，
再复制配置并添加：

```text
--confirm-paid-api --confirm-holdout
```

锁定集只运行一次，不根据结果回头修改相同候选再重测。

## 真实开发候选与指纹冻结

`qwen-e2e-rag-v1`完成3次真实Embedding业务请求，消耗5,040个输入Token；13条通过范围门且检索非空的
问题各执行一次`qwen-plus`结构化回答。结果如下：

| Profile | 检索Recall | Top-1 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Hash + BM25 + Extractive | 100% | 90.91% | 100% | 60% | 87.5% | 40% | 100% | FAIL |
| qwen-e2e-rag-v1 | 100% | 100% | 100% | 80% | 93.75% | 20% | 100% | **PASS** |

候选修复了“数字人民币/货到付款”知识缺口，但仍错误回答“普通退货能否免费安排快递上门取件”。该题在
第28步开发集也曾失败，继续作为Bad Case保留；不为追求开发集100%增加关键词特判或删除样本。

开发指标正好达到正确拒答80%和无依据回答率20%的预设边界，属于**压线通过而不是高余量通过**。完整候选
已经冻结为：

```text
197c37b7c7e888a685eb5e4d1a79b99b648da11f04311cdbe1a52bd61f649f47
```

冻结以后，任何语料、切片、Embedding模型/维度、阈值、Top-K、BM25权重、Profile或Grounded系统提示变化
都会造成指纹不匹配并禁止运行holdout。之后保持当前实现不变，只运行了一次12条新锁定集。

## 一次性端到端锁定结果

冻结候选完成4次Embedding业务请求、5,340个输入Token和22次实际聊天调用；其中开发链路13次，锁定链路
9次。12条锁定题包含8条可回答和4条知识缺口，结果如下：

| Profile | 检索Recall | Top-1 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Hash + BM25 + Extractive | 100% | 100% | 100% | 50% | 83.33% | 50% | 100% | FAIL |
| qwen-e2e-rag-v1 | 100% | 100% | 100% | 100% | 100% | 0% | 100% | **PASS** |

当前链路错误回答了“礼品卡余额有效期”和“周末指定九点送货”两个知识缺口。它们都找到了词面或主题相近
的文档，因此检索Recall和Top-1仍显示100%；这些检索指标只以8条正例为分母，不会替系统回答4条负例的
行为背书。Grounded候选对两个近领域缺口均转人工，并保持8条知识内问题全部回答和引用合法。

候选因此通过端到端晋级门，可作为**已验证的真实模型Profile**使用。本项目仍保留Hash+Extractive为默认
本地/CI Profile，保证无Key、零费用和确定性演示；“晋级”表示真实Profile获得配置和简历证据资格，不表示
删除回退方案，也不表示12条锁定题可以代表线上全部问题。

## 结论边界

1. 锁定集只有12条，100%是8/8正例回答、4/4负例拒答，不是大规模线上准确率；
2. 知识语料只有12份活动文档和23个Chunk，未覆盖真实企业的更新频率、权限层级与长文档分布；
3. 开发集仍有“免费上门取件”Bad Case，说明同一模型面对不同表达并非永久稳定；
4. 真实上线仍需要持续收集线上失败、扩充时间切分测试、监控拒答率和设置人工接管。

## 当前面试表达

> 我先分别验证范围门、Rerank、语义召回和证据充分性，再建立端到端晋级门，避免局部指标代替整体效果。
> 当前Hash+BM25+Extractive虽然在端到端锁定集的正例检索Recall和Top-1都是100%，仍错误回答两个近领域
> 知识缺口，无依据回答率50%。组合qwen3.7语义召回与Grounded判断后，8条知识内题全部回答，4条知识
> 缺口全部拒答，无依据回答率降到0%，引用合法率保持100%。该结果只对应12条一次性锁定题；开发集仍保留
> 一条上门取件Bad Case，因此我没有把它外推为线上零幻觉。

## 权威资料

- [LangChain Retrieval](https://docs.langchain.com/oss/python/langchain/retrieval)
- [LangChain Structured Output](https://docs.langchain.com/oss/python/langchain/models#structured-output)
- [Qdrant Hybrid Search与Reranking](https://qdrant.tech/documentation/tutorials-basics/reranking-hybrid-search/)
