# 第39步：语义完整性Judge校准

## 为什么不能继续补关键词

第38步自动失败的10道题经过人工复核后，8道是同义改写、插入词、词序或否定关系让机械短语匹配漏判，
2道是金标要求了用户没有询问的背景信息。继续把这10道答案里的说法逐条追加到词表，虽然能把已揭晓分数
调高，却不能证明下一种同义表达也能识别，属于对测试集过拟合。

第39步因此把评测职责拆开：

- 确定性规则继续负责引用越界、无依据自动回答、禁止事实等安全红线；
- 语义Judge只处理“答案是否完整回答用户明确子问题”这一类机械匹配难题；
- Judge不得覆盖确定性红线，也不能把人工复核30/30冒充新的盲测成绩。

可以把它理解成：门口的安检仍由严格规则负责，不能因为AI说“应该没事”就放行；只有“这段回答意思是否
完整”这种需要理解语义的问题，才交给经过考试的Judge。

## Judge看到什么

Judge每次只收到：

- 用户问题；
- 最终用户可见答案；
- 模型实际引用的证据正文。

它看不到 `expected_pass` 人工标签，也看不到“这条应该判对”的提示。结构化输出必须明确回答：是否覆盖所有
子问题、是否完全受证据支持、是否存在矛盾，以及最终PASS/FAIL/NEEDS_REVIEW。PASS与三个布尔字段不一致时，
Pydantic会在本地拒绝这份模型响应。

## 为什么要加入反例

人工复核确认正确的10条争议原答案是正向校准项。每条再配一条固定的“不回答问题”反例，共20条。

如果Judge偷懒把所有输入都判PASS：

- 10条原答案会判对；
- 10条反例会判错；
- 最终一致率只有50%，无法通过90%质量门。

项目仍只突出一个指标：`语义Judge人工一致率 = 与人工标签一致的项数 / 20`。90%门槛在真实调用前已经
冻结，不会看到结果后降低。

## 成本与隐私

第39步复用第38步已经生成并保存的原答案与证据：

- Embedding调用：0次；
- Agent答案生成：0次；
- 语义Judge计划调用：20次；
- 私有问题、答案、证据和Judge简短理由不会进入公开Report；
- 公开结果只保存Case ID、变体、预期、预测和有限原因码。

Judge本身仍可能犯错，并且当前使用同一服务商的 `qwen-plus`，存在同源模型偏差。所以校准通过只代表它在
这20条人工样本上达到预设一致率，不代表可以完全取代人工。后续生产使用仍应抽检，并为新题建立新的校准集。

## 已完成的零费用验证

- 来源私有诊断SHA、Agent候选指纹和公开人工审计SHA全部匹配；
- 20条校准项成功装配，其中10条正向、10条负向；
- Judge候选指纹冻结为
  `d2c395db0d47b3b59cfe86a35c9f02dede0dae5dab53eee8ee69850efddff160`；
- 默认运行不读取私有诊断、不读取API Key、不创建模型客户端；
- 只确认私有回归时Embedding、Agent生成和Judge调用都为0。

## 首次真实校准结果

真实 `qwen-plus` Judge在20条校准项上与人工标签一致20/20，一致率100%，通过揭晓前冻结的90%质量门：

- 10条人工确认正确的争议原答案全部判PASS；
- 10条固定不完整反例全部判FAIL；
- Embedding调用0次；
- Agent答案生成0次；
- 实际Judge调用20次；
- 公开结果不含问题、答案、证据或Judge理由正文。

首次冻结结果SHA为
`5f0b29646d4b1ae0acb734df02ad794e114d099ef1cb1cb666dcfba9eb62a4a4`。

这里的100%是**Judge在20条人工校准项上的一致率**，不是Agent在新盲测上的成功率，也不代表Judge可以永久
替代人工。正式Agent盲测仍是第38步20/30（66.67%）且Gate FAIL；第38步已揭晓回答的人工语义复核30/30
也仍然只是审计结论。三个数字必须分开表述。

## PyCharm运行

运行配置填写：

- Script path：`D:\serviceops-agent\examples\39_semantic_judge_calibration.py`
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`

零费用装载检查参数：

```text
--confirm-private-regression
```

确认显示20条、Embedding 0次、Agent生成0次后，首次真实Judge校准参数为：

```text
--confirm-private-regression --confirm-paid-api
```

首次结果已经独占写入
`data/evaluation/results/semantic_judge_calibration_v1_result.json`。文件存在后，同一入口会拒绝重复付费运行，
避免多跑几次后只挑最好看的结果。

## 面试时怎样讲

不要说“用了LLM-as-a-Judge，所以评测更高级”。应该说：

> 确定性事实评分在否定句、插入词和同义改写上召回不足，但安全红线不能交给概率模型。我保留规则负责引用、
> 禁止事实和无依据回答，只把答案完整性争议交给结构化语义Judge；再用10条人工正确答案与10条不完整反例
> 做盲标签校准，防止Judge全部判通过。这样解决的是评测有效性问题，而不是为了堆叠技术名词。

## 本步文件

- Judge实现：`src/serviceops_agent/evaluation/semantic_judge_calibration.py`
- 公开冻结配置：`data/evaluation/semantic_judge_calibration_v1.json`
- 运行入口：`examples/39_semantic_judge_calibration.py`
- 单元测试：`tests/unit/test_semantic_judge_calibration.py`
- 本机脱敏报告：`data/runtime/semantic_judge_calibration_report.json`
