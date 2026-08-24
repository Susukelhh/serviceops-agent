# 第38步：范围门v2全新密封盲测

## 为什么必须再建一套新题

第36步题目已经揭晓，后续任何修复都只能在那批题上做开发回归，不能再证明系统面对未知问题时是否有效。
因此第38步重新建立30道此前未参与调参的密封题，并在看结果前冻结题集SHA与候选指纹。

这一步只验证上一轮审计指出的两个系统问题：

- 范围门从 `deterministic_v1` 升级为 `deterministic_v2`，区分“索取密码”和“咨询是否应该提供密码”；
- 拒答时按照生产系统最终给用户看的状态清空引用，再做事实级评分。

Prompt仍然使用v2，Embedding、切片、Qdrant语义召回、BM25、RRF、Top-K和聊天模型参数均保持不变。
这样如果结果发生变化，才能说清楚主要是哪个改动造成的，而不是一次改十个参数后无法解释。

## 揭晓前冻结的契约

- 题量：30题，其中20题有公开证据、10题是真正知识缺口；
- 题集SHA：`605a83136532d6942724660b8ed8e12f185d49f1d908e92830a6ca2cf767c95b`；
- 候选指纹：`beefd956668ad87bb40ed8923a8111ef08d4e122feec6a59f839c6ba3e314006`；
- 唯一主指标：端到端有据回答成功率；
- 晋级门：成功率至少80%，并且禁止事实、无依据回答、非法或不受支持引用三类红线均为0；
- 私有题目与金标只保存在 `data/private_evaluation/`，不会进入Git或Docker镜像。

每道正例只有同时满足“范围门放行、检索到证据、最终答案覆盖用户所问事实、引用真实支持该事实、没有
矛盾结论”才得1分。知识缺口只有最终拒答且不向用户展示引用才得1分，任何一层失败都是0分。

## 已完成的零费用校验

离线运行已经确认：

- 私有题集结构为30题、20正例、10负例；
- 所有正例事实锚点都能在真实500/80切片中找到；
- 范围门v2没有提前拒绝任何正例；
- 候选运行时重新计算的指纹与冻结指纹一致；
- Hash + BM25 + RRF + Extractive离线对照为16/30（53.33%），10条知识缺口均暴露出机械摘抄基线会被相似文档带偏。

离线对照只是风险参照，不能代表千问候选效果。

## 首次真实密封结果

首次千问候选严格通过20/30，有据回答成功率66.67%，红线为0，但低于揭晓前写死的80%门槛，因此
质量门为FAIL。该结果已独占冻结，不能被后续回归覆盖。

进一步拆分可见：

- 10道知识缺口全部正确拒答，没有无依据自动回答；
- 20道有公开证据的问题完整通过10道；
- 其余10道全部是 `required_fact_missing`，没有禁止事实、越界引用或不受支持引用；
- 安全咨询正例已通过，说明范围门v2解决了第36步发现的误拒问题。

所以当前瓶颈不是“模型乱编”，而是“最终答案是否完整覆盖用户明确询问的条件”。但脱敏冻结报告没有保存
原始回答，暂时无法确定这10道失败是模型真的漏说，还是确定性短语评分器漏掉了同义表达。不能看到分数后
直接修改Prompt。

首次冻结结果SHA为
`6f477868c65f32a4be53b7518b263f7ae9da71f1540a469f906aaf2e07db8248`。

## PyCharm运行方式

新建一个Python运行配置：

- Script path：`D:\serviceops-agent\examples\38_grounded_answer_v3_sealed.py`
- Working directory：`D:\serviceops-agent`
- Python interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`

先做零费用离线确认，Parameters填：

```text
--confirm-sealed
```

确认输出中的题集SHA和候选指纹与本文一致后，首次真实密封实验才填：

```text
--confirm-sealed --confirm-paid-api
```

真实运行预计产生约4次Embedding请求、最多30次聊天请求。该命令必须由项目所有者本人运行；助手不会代为
调用付费API。首次结果会独占写入
`data/evaluation/results/grounded_answer_v3_sealed_result.json`，文件存在后同一入口会拒绝再次付费运行。

首次题集已经揭晓。若要定位10道 `required_fact_missing`，只能明确运行一次付费REGRESSION并把原始诊断留在
本机私有目录：

```text
--confirm-sealed --confirm-paid-api --regression --write-private-diagnostics
```

该命令会再次产生约4次Embedding和最多30次聊天调用。REGRESSION前后程序会校验首次结果的内容SHA、字节数
和修改时间，任何变化都会停止发布新报告；私有诊断不会进入Git或Docker。由于模型复跑可能有轻微变化，
诊断只解释这一轮回归，不能无损还原首次20/30中的原始回答。

## 已揭晓回归诊断结论

付费REGRESSION再次得到20/30且失败ID完全一致，说明自动失败具有稳定性。逐题对照原问题、模型答案、实际
引用和事实规则后，将10题分类为：

- 8题 `MATCHER_FALSE_NEGATIVE`：模型已经表达正确含义，但同义词、插入修饰词、词序或否定关系导致机械
  子串评分没有命中；
- 2题 `GOLD_SCOPE_TOO_STRICT`：模型已经回答用户的问题，但金标额外要求了用户没有明确询问的背景说明；
- 0题 `MODEL_OMISSION`；
- 0题 `AMBIGUOUS_NEEDS_REVIEW`。

因此已揭晓回答的人工语义复核为30/30，但它**不是新的盲测成绩**，不能覆盖正式20/30，也不能把项目写成
“盲测100%”。该结论说明没有证据支持继续修改Prompt；真正需要改进的是评测器的语义召回能力与金标范围
治理。公开脱敏审计保存在
`data/evaluation/results/grounded_answer_v3_sealed_regression_audit.json`。

后续评测应保留确定性规则检查引用、禁止事实和无依据回答等高风险红线；对于答案完整性，可以引入经过人工
样本校准的语义判定器，但必须保存判定理由并抽检，不能让另一个LLM分数直接替代人工真值。正式泛化结论仍需
使用下一套从未揭晓的密封集。

## 如何解释结果

本次Gate已经FAIL，退出码1表示实验结论未达到预设80%，不等于代码崩溃。必须保留首次结果，再把已揭晓
失败题用于诊断；
任何继续修复后的验证都要换下一套密封题，不能在本题集上反复调到高分后仍称“盲测提升”。

## 本步文件

- 公开冻结配置：`data/evaluation/grounded_answer_v3_sealed_experiment.json`
- 私有密封题集：`data/private_evaluation/grounded_answer_success_v3.json`
- 一次性入口：`examples/38_grounded_answer_v3_sealed.py`
- 安全与契约测试：`tests/unit/test_grounded_answer_v3_sealed.py`
- 本机脱敏报告：`data/runtime/grounded_answer_v3_sealed_report.json`
