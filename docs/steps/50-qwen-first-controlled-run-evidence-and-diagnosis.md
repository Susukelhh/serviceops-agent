# 第50步：真实千问首次受控运行的证据归档与失败诊断

## 这一步解决什么问题

第49步已经能重复运行真实千问并输出低敏报告，但一个普通JSON文件仍有三个治理缺口：

1. 总通过率和`promotion_gate_passed`可能被运行后手工修改；
2. 报告没有强绑定当时的数据集、实验配置、源码revision和模型身份；
3. 同一路径再次运行可能覆盖旧结果，导致“首次结果”无法审计。

第50步把候选报告升级为证据包：

```text
第49步低敏报告
  + 原始数据集字节
  + 原始实验配置字节
  + Git source revision
  + workflow run ID / attempt
       ↓
重新计算全部指标与晋级结论
       ↓
核对版本化费用预算
       ↓
计算SHA-256与候选指纹
       ↓
生成低敏失败诊断
       ↓
exclusive-create不可覆盖证据包
```

这一步不再调用LLM，因此归档失败可以安全重试；但同一证据文件不能覆盖，只能使用新的run ID生成新证据。

## 为什么必须重新计算报告

报告中的聚合字段不是可信输入。归档器只把逐trial、逐turn的有限结果当作重算基础，然后再次调用第49步的聚合器，重新得到：

- 平均轮次通过率；
- 最差场景通过率；
- 全轮稳定场景率；
- 跨trial波动率；
- 安全准确率；
- 全部晋级失败码；
- 最终`promotion_gate_passed`。

重算结果必须与提交归档的完整报告逐字段一致。测试把`mean_turn_pass_rate`手工改成`0.123`时，归档会抛出“与逐轮结果重新计算值不一致”，不会生成证据文件。

需要注意：这能阻止简单手改聚合字段，但不能证明产生逐轮结果的执行主机绝对可信。更高保障需要受保护的GitHub Environment、OIDC签名、不可变对象存储和组织级审计日志。

## 证据绑定哪些身份

Manifest保存以下SHA-256：

```text
report_sha256   第49步原始报告字节
dataset_sha256  多轮人工金标原始字节
config_sha256   实验场景、trial、预算与阈值原始字节
```

`candidate_fingerprint`再对以下字段做规范JSON哈希：

```text
candidate_model
candidate_profile
dataset_sha256
config_sha256
source_revision
```

因此模型名、场景、阈值、预算、源码revision中任一项变化，候选指纹都会变化，旧结果不能被当成新候选的晋级证据。

## 为什么还要保存run ID和source revision

`run_id`回答“是哪一次执行”，GitHub Actions使用：

```text
github-<run_id>-<run_attempt>
```

重跑同一个workflow时`run_attempt`会变化，不会与首次执行混淆。

`source_revision`回答“运行的究竟是哪份代码”。Actions传入完整`${{ github.sha }}`；本地运行必须显式传入当前提交或受控分支revision，归档器不会从报告正文猜测来源。

## 费用预算再次验证

归档器要求同时满足：

```text
planned_chat_calls_per_trial × trial_count
    == planned_total_chat_calls

planned_total_chat_calls
    <= config.max_planned_chat_calls

report.budget_limit_chat_calls
    == config.max_planned_chat_calls
```

第49步是在创建模型客户端前阻止超预算运行；第50步是在归档时防止报告把预算字段改小或把超预算执行包装成合规证据。两层校验目的不同。

## 失败诊断如何做

诊断器只聚合有限失败码，不读取或复制问题和回答：

| 失败码 | 建议排查路径 |
|---|---|
| `context_resolution_mismatch` | 指代解析和输入记忆 |
| `model_behavior_mismatch` | 千问分类、规划或Grounded Generation |
| `memory_projection_mismatch` | 可信终态到结构化记忆的投影 |
| `safety_invariant_failed` | 立即阻止晋级，检查越权、引用或审批前写入边界 |

场景还会分成两类：

- `persistent_failure_scenario_ids`：每个trial都失败，通常是系统性标签、Prompt或链路问题；
- `intermittent_failure_scenario_ids`：有时通过、有时失败，通常是模型波动、限流降级或边界表达不稳定。

这个诊断只负责“把人带到正确层”，不能代替受控环境中的原始Trace排查，也不会把完整模型输出上传到公开Artifact。

## 不可覆盖归档如何实现

证据包是单个JSON文件，内部同时包含Manifest和第49步低敏报告：

```text
<run_id>__<candidate_fingerprint前12位>.json
```

写入使用Python文件模式`x`，也就是底层exclusive-create：目标已存在就抛`FileExistsError`。单文件设计避免“Manifest写完但报告文件没写完”的双文件不一致。

本地默认目录为：

```text
data/runtime/qwen_multi_turn_evidence/
```

该目录受`.gitignore`保护，不把真实运行产物提交到源码仓库。GitHub Actions把证据包作为独立Artifact保存30天，普通候选报告保存14天。

30天Artifact不是永久合规存档。正式生产发布应把同一证据包复制到启用版本控制、保留策略和写后不可变能力的对象存储；本项目当前没有擅自接入外部存储。

## 本地归档命令

先显式运行第49步真实实验并生成报告，随后执行：

```powershell
.venv\Scripts\python.exe examples\50_archive_qwen_multi_turn_evidence.py `
  --report data/runtime/qwen_multi_turn_experiment_report.json `
  --run-id local-20260830-001 `
  --source-revision <当前Git提交SHA>
```

如果报告不存在、JSON结构错误、数据集/配置身份不一致、预算不一致、聚合字段被改动或目标文件已存在，命令都会返回非零退出码。

第50步脚本没有`--confirm-paid-api`，因为它绝不创建LLM客户端。付费授权只属于第49步运行脚本，不能扩散到后处理工具。

## GitHub Actions执行顺序

手工工作流现在依次执行：

```text
第14步：独立单轮真实候选
第49步：共享会话真实候选
第50步：重算、诊断、指纹和不可覆盖归档
上传普通报告
上传证据包
```

第50步使用`if: always()`加报告存在条件。即使第49步因为晋级门失败返回退出码1，仍然会归档这次失败证据，避免只保存成功结果形成幸存者偏差。原第49步失败状态仍会让整个Job失败，不会被归档步骤“洗成成功”。

工作流仍然只有`workflow_dispatch`，不响应push或pull request；真实模型Secret仍不会进入普通PR质量门。

## 负向控制

本步包含三类零费用测试：

1. 正常报告重算一致，三份SHA与候选指纹生成成功；
2. 手改聚合值必须被拒绝；
3. 一次通过、一次失败的场景必须诊断为`intermittent`，并路由到模型行为排查；
4. 同一run ID和候选指纹第二次归档必须触发`FileExistsError`。

测试还检查证据包不包含数据集中的问题正文或`assistant_answer`字段。

## 本步文件

- 证据与诊断模块：`src/serviceops_agent/evaluation/qwen_multi_turn_evidence.py`
- 公开评测接口：`src/serviceops_agent/evaluation/__init__.py`
- 归档CLI：`examples/50_archive_qwen_multi_turn_evidence.py`
- 手工付费工作流：`.github/workflows/qwen-candidate-evaluation.yml`
- 证据正负控制：`tests/unit/test_qwen_multi_turn_evidence.py`
- Actions边界测试：`tests/unit/test_ci_workflows.py`

## 当前结论

第50步完成的是“首次真实结果应当怎样产生、验证、诊断和保存”。当前仍未获得本轮用户对真实付费API调用的明确授权，也没有可归档的第49步真实报告，因此没有伪造证据文件，更不能声称千问已经晋级。

要得到第一份真实证据，应由用户在GitHub Actions页面手工运行`ServiceOps Qwen candidate experiment`，或明确授权本地使用已配置的千问额度。运行结束后再根据证据包判断通过、拒绝或进入失败修复循环。

## 本步验证结果

2026-08-30完成全库门禁：

```text
Ruff  : All checks passed
Mypy  : Success, 102 source files
Pytest: 403 passed, 5 skipped
diff  : git diff --check passed
```

新增测试全部使用合成低敏结果，不读取API Key、不连接千问，也不产生付费请求。5个跳过项仍是需要外部PostgreSQL测试DSN等可选环境的集成用例。
