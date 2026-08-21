# 第 28 步：证据充分性与无依据回答控制

## 为什么检索到文档仍然不能直接回答

第27步的真实语义向量把8条锁定正例全部排到第一位，却把“是否支持数字人民币和货到付款”错误匹配到
“数字商品兑换规则”。它们主题相似，但文档没有提供支付方式答案：

```text
相关性：问题和文档在谈相近主题
充分性：文档包含足以回答问题的明确事实
```

相关不等于充分。继续提高向量阈值会损失正确召回，BM25/Rerank也只能换顺序，因此本步评测检索后的第三道门。

## 单变量实验设计

每条样本固定“问题＋已经召回的真实Chunk”，两个回答器看到完全相同的证据：

- Extractive：只要证据非空就组织正文；
- Qwen Grounded：返回`answer`、`citation_ids`、`is_answerable`三个Pydantic字段；
- LangGraph节点再确定性检查引用ID必须属于本次证据白名单；
- `is_answerable=false`、无引用或越界引用全部转人工。

开发集有16题：8条证据真正包含答案，8条只有主题相似。新的10条holdout在提示冻结前不运行。第27步失败题
已经揭晓，因此只能进入本轮开发回归，不能再次冒充未知holdout。

## 五项指标

| 指标 | 通俗解释 | 防止什么问题 |
|---|---|---|
| Answerable Recall | 真有答案时成功回答的比例 | 全部拒答假装安全 |
| Abstention Accuracy | 没答案时正确转人工的比例 | 答非所问 |
| Decision Accuracy | 全部回答/拒答决策准确率 | 只看单侧指标 |
| Unsupported Answer Rate | 知识缺口仍自动回答比例 | RAG幻觉 |
| Citation Validity | 放行答案引用均来自候选白名单 | 伪造引用 |

## 离线失败基线

| 回答器 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---|
| Extractive | 100% | 0% | 50% | 100% | 100% | FAIL |

引用合法100%不等于答案正确：Extractive确实引用了本次候选，但候选正文没有回答问题。这正是“有引用”和
“有依据”必须分开评测的原因。

## 费用与holdout保护

- 默认脚本只运行Extractive，费用0元；
- `--confirm-paid-api`才会对16条开发题各调用一次`qwen-plus`；
- 报告保存Grounded系统提示SHA-256；
- 开发质量门通过后，把该指纹写入`frozen_prompt_sha256`；
- 只有指纹仍匹配并提供`--confirm-holdout`，才读取新的10条锁定题。

结构化输出让程序取得经过Schema校验的对象，而不是从自然语言中猜True/False；具体机制参考
[LangChain官方Structured Output文档](https://docs.langchain.com/oss/python/langchain/models#structured-output)。

## PyCharm离线验证

- Name：`28 Grounding baseline`
- Script path：`D:\serviceops-agent\examples\28_grounding_sufficiency_experiment.py`
- Parameters：留空
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- Environment variables：`PYTHONUTF8=1`

真实开发候选复制该配置，名称改成`28 Qwen grounding development`，Parameters填写：

```text
--confirm-paid-api
```

计划16次聊天调用。第一次真实运行即使代码成功，也可能因提示尚未冻结而以退出码1结束。

## 真实开发候选与提示冻结

`qwen-plus`按计划完成16次结构化调用，结果如下：

| 回答器 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---|
| Extractive | 100% | 0% | 50% | 100% | 100% | FAIL |
| Qwen Grounded v1 | 100% | 87.5% | 93.75% | 12.5% | 100% | **PASS** |

候选在8条知识内题上全部回答并使用合法引用，在8条知识缺口中正确拒答7条。唯一失败是
`ground-dev-gap-home-pickup`：用户询问是否免费安排快递上门取退货，候选错误地认为普通退货和运费规则足以
回答。该Bad Case保留，不删除、不改标签，也不为了开发集100%临时修改提示。

当前系统提示SHA-256已经冻结为：

```text
1c5a43de5b8f50dc4849911527fc233aa0b6aefa0197697b18382e2b48ccad4d
```

该指纹与通过开发门的代码完全一致。之后只运行一次全新holdout，且没有根据锁定结果回头修改提示或标签。

## 一次性锁定集结果

冻结提示后，对10条此前未运行的锁定题执行一次验收：5条证据真正包含答案，5条只有相近主题但缺少答案。
本次开发集与锁定集合计完成26次真实结构化聊天调用。

| 回答器 | 有答案召回 | 正确拒答 | 综合决策 | 无依据回答 | 引用合法 | Gate |
|---|---:|---:|---:|---:|---:|---|
| Extractive | 100% | 0% | 50% | 100% | 100% | FAIL |
| Qwen Grounded v1 | 100% | 100% | 100% | 0% | 100% | **PASS** |

候选在5条知识内题上全部回答，在5条知识缺口上全部转人工，没有出现越界引用，锁定集失败样本为0条。
因此，`qwen-grounded-answer-v1`通过了**固定证据回答层**的晋级门。

## 结论边界

这组100%不能解释成“线上RAG幻觉率为0”，原因有三点：

1. 锁定集只有10条，是专项工程回归集，不代表真实用户问题的完整分布；
2. 实验固定了每题证据，没有把Embedding误召回、Top-K排序和Chunk遗漏混入回答层指标；
3. 模型仍可能在新表达、新政策或更复杂的多文档问题上犯错，因此线上仍保留拒答、转人工和持续回归。

下一步必须把真实检索链路重新接回：`范围门 → Embedding召回 → 候选重排 → 证据充分性判断 → 引用校验`，
使用新的端到端开发集和锁定集验证整体效果，之后才能决定是否切换默认RAG配置。

## 最终面试表达

> 语义Embedding虽然在锁定正例上Recall和Top-1达到100%，仍把支付方式问题误召回到数字商品规则，说明
> 相关不等于可回答。我没有继续调已揭晓数据，而是固定真实Chunk建立证据充分性专项集。旧Extractive
> 回答器在10条锁定题上的无依据回答率为100%；加入Pydantic结构化`is_answerable`、引用白名单和人工兜底后，
> `qwen-plus`候选将无依据回答率降到0%，同时保持有答案召回、正确拒答和引用合法率100%。这只是固定证据
> 小型锁定集的结果，因此下一步仍需通过完整检索链路的端到端质量门。
