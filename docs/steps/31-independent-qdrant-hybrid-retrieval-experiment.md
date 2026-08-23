# 第31步：独立 Qdrant 与完整混合召回对照实验

## 先说结论

独立 Qdrant 和全语料 BM25 + RRF 已经能稳定运行，但“技术接上了”不等于“质量一定更好”。本步用真实
对照得出两个不同结论：

1. 在 74 条已揭晓开发题上，冻结 RRF 把 Top-1 从纯向量的 83.87% 提升到 93.55%，比旧候选内重排的
   91.94% 再提高 1.61 个百分点；
2. 在 16 条新锁定题上，12 条知识内问题的 Recall@5 为 100%、Top-1 为 91.67%，但 4 条域外问题误放行
   2 条，FPR 为 50%，因此整体晋级门 **FAIL**。

所以当前生产配置不切换到开发优胜的关键词权重 1.50。锁定结果说明下一步应该补强意图/范围识别，而不是
继续盲调 RRF。

## 四条路线像什么

把知识库想成一座图书馆：

| 路线 | 大白话 |
|---|---|
| Dense only | 只按“意思像不像”找书 |
| Legacy candidate BM25 | 先按意思拿 5 本，再在这 5 本里看关键词 |
| Full-corpus BM25 | 不看向量，直接在整座图书馆按关键词找书 |
| Hybrid RRF | 前两名检索员各自查完整图书馆，再按各自名次合并榜单 |

旧版候选重排永远救不回向量 Top-5 之外的书；完整混合召回具备这个能力。但当前语料只有 12 份公共文档、
23 个切片，纯向量 Recall@5 已经是 100%，所以本次 `lexical_rescue_cases=0`。这次实验能证明的是排名改善，
不能写成“召回率提升”。

## 公平实验怎么做

四条路线共用以下变量：Hash Embedding 1024 维、Chunk 500/Overlap 80、向量阈值 0.10、Top-5、同一份
受治理语料和同一版确定性范围门。开发集由此前已经揭晓的 4 组数据合并，共 74 条；这些旧 holdout 已经
失去未知性，因此诚实地降级为开发回归集。另建 16 条新锁定题，冻结参数前默认不读取。

RRF 扫描：`k ∈ {30, 60}`，关键词权重
`{0.40, 0.80, 1.00, 1.20, 1.50, 2.00}`，向量权重固定 1.0。开发优胜者为：

```text
hybrid-rrf-k60-lex1.50
```

`k=30` 与 `k=60` 指标同分，保留现有工程默认 `60`，避免为了没有收益的参数变化增加配置漂移。

## 开发结果

| Profile | Recall@5 | Top-1 | MRR@5 | nDCG@5 | FPR |
|---|---:|---:|---:|---:|---:|
| Dense only | 100% | 83.87% | 91.18% | 93.44% | 0% |
| Legacy candidate BM25 | 100% | 91.94% | 95.97% | 97.02% | 0% |
| Full-corpus BM25 | 100% | 88.71% | 94.35% | 95.82% | 0% |
| Frozen Hybrid RRF | 100% | **93.55%** | **96.77%** | **97.62%** | 0% |

逐题审计显示 7 条正确文档排名前移、1 条后退。平均分提升不是“每一道题都变好”，保留退化样本才能继续
分析权重过高的代价。

## 一次锁定结果为什么失败

冻结后只运行一次 16 条新题：

| Profile | Recall@5 | Top-1 | MRR@5 | FPR | Gate |
|---|---:|---:|---:|---:|---|
| Frozen Hybrid RRF | 100% | 91.67% | 95.83% | **50%** | **FAIL** |

两个失败不是知识内问题排错，而是范围门没有识别新的域外表达：

- “杭州明后两天有雨吗”没有出现旧规则中的“天气/下雨/降雨”；
- “写一篇考试作文”没有出现旧规则中的“写诗/翻译/代码”。

这解释了为什么混合召回和意图识别不能互相替代：RRF 负责“资料怎么排”，范围门负责“这件事该不该进
图书馆”。错误问题一旦被放进图书馆，检索算法通常总能找到一些表面相似的资料。

## 运行方法

PyCharm 配置：

- Script path：`D:\serviceops-agent\examples\31_hybrid_retrieval_experiment.py`
- Parameters：开发复测留空；历史锁定结果不要反复重跑
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- Environment variables：`PYTHONUTF8=1`

锁定集已经执行并转为历史证据。以后若修改范围识别，应把这 16 条题降级为开发回归集，另建新的未知
holdout，不能在同一张考卷上反复调到 100%。

## 面试表达

> 我没有因为接入 Qdrant、BM25 和 RRF 就直接写“召回率提升”。我建立四路同变量对照，在 74 条开发集上
> 把 Top-1 从纯向量 83.87% 提升到 93.55%，比旧候选内重排再提升 1.61 个百分点；但纯向量 Recall@5
> 已经 100%，关键词救回数为 0，所以收益应定义为排序改善。冻结参数后，16 条新锁定集虽然知识内检索
> 通过，却有两条新域外表达被范围门放行，FPR 50%，最终拒绝晋级。这说明实际难点不是堆检索组件，而是
> 划清“问题该不该检索”和“检索后证据怎么排”两类责任；下一版会用失败样本驱动意图分类评测，并换新
> holdout 验证，绝不在同一锁定集上刷分。

## 代码入口

- `src/serviceops_agent/rag/hybrid.py`
- `src/serviceops_agent/evaluation/rag_hybrid_experiment.py`
- `examples/31_hybrid_retrieval_experiment.py`
- `data/evaluation/rag_hybrid_experiment.json`
- `data/evaluation/results/rag_hybrid_v1_frozen_result.json`

## 资料

- [Qdrant：Hybrid Search 与 Reranking](https://qdrant.tech/documentation/tutorials-basics/reranking-hybrid-search/)
- [Qdrant：RRF Query API](https://qdrant.tech/documentation/concepts/hybrid-queries/)
