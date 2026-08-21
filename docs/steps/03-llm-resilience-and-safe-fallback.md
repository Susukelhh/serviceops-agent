# 第三步：模型故障分类、脱敏日志与安全降级

## 本步目标

真实 LLM 是不可靠的外部依赖：密钥会失效、额度会耗尽、网络会超时，模型也可能返回不符合
Schema 的内容。本步不追求“永远不出错”，而是确保这些错误不会击穿 LangGraph 和 FastAPI，
也不会诱使系统在没有可靠分类结果时继续调用业务工具。

## 新增故障调用链

```text
千问/OpenAI兼容SDK异常
→ normalize_llm_exception归一化
→ LLMServiceError（有限类别、无原始响应正文）
→ LLM分类节点记录脱敏日志
→ intent强制设为human_handoff
→ LangGraph条件边进入人工节点
→ API返回可处理的业务响应
```

正常模型调用链没有改变；只有外部模型失败时才进入这条分支。

## 为什么要做异常归一化

如果 LangGraph 节点直接捕获 `AuthenticationError`、`RateLimitError` 等 SDK 异常，业务控制流
就会依赖某一家供应商。未来接入另一个模型时，所有节点都可能被迫修改。

现在由 `llm/errors.py` 把具体 SDK 异常翻译为有限内部类别：

| 内部类别 | 典型原因 | 是否适合稍后重试 |
|---|---|---:|
| `authentication` | Key 无效、撤销、地域或 Base URL 不匹配 | 否 |
| `rate_limit` | 额度、并发或速率限制 | 是 |
| `timeout` | 超过客户端超时时间 | 是 |
| `connection` | DNS、TLS、代理或网络中断 | 是 |
| `invalid_response` | 结构化输出或 Pydantic 校验失败 | 否 |
| `upstream` | 其他非 2xx 服务商响应 | 否（当前保守策略） |
| `unknown` | 适配器边界中的未知第三方异常 | 否 |

`retryable` 是策略元数据，不等于“立刻在节点中重试”。盲目重试会增加延迟和 Token 费用，
还可能在服务商故障时放大流量。后续只有结合幂等性、退避和总耗时预算后才会增加重试策略。

## 为什么故障后不自动切回关键词分类器

关键词分类器适合作为离线基线和无密钥开发模式，但生产请求已经选择 LLM 通道时，静默切回
弱规则可能把未知问题误判成订单查询并继续执行工具。当前系统选择更保守的策略：

```text
没有可靠分类结果 → 不执行工具 → 转人工
```

这是 fail-safe，而不是 fail-open。

## 日志为什么不保存原始异常正文

服务商错误正文可能包含请求片段、端点、账号信息，极端情况下还可能包含凭证。当前日志只记录：

- `request_id`：关联一次请求；
- `kind`：有限故障类别；
- `retryable`：是否适合稍后重试；
- `cause_type`：原始 Python 异常类名。

日志不记录 API Key、用户原文和服务商响应正文。原始异常仍通过 Python 异常链保留给本地调试器，
但不会写入 LangGraph State 或 FastAPI 响应。

## 为什么当前返回 HTTP 200

模型故障后，系统仍然成功完成了一项业务决策：停止自动化并请求人工接管。因此当前 API 使用
`requires_human=true` 表达处理结果，而不是让调用方收到无法解析的 500。

生产版本还会增加独立 readiness、错误率指标和告警。届时“用户请求得到安全处理”和“模型依赖
处于健康状态”会由不同信号表达，不能因为 HTTP 200 就忽略运维故障。

## 在 PyCharm 中运行

直接运行：

```text
examples/03_llm_failure_fallback.py
```

或者在 Terminal 执行：

```powershell
uv run python examples/03_llm_failure_fallback.py
```

重点观察：

1. `intent` 是否为 `human_handoff`；
2. `llm_failure_code` 是否为 `connection`；
3. 事件中是否包含 `graph:llm_connection_fallback_to_human`；
4. `tool_name` 是否不存在；
5. 控制台是否没有 Python Traceback。

该示例完全不访问千问，不消耗 API 额度。

## 本步测试

- OpenAI 兼容认证、限流、超时和通用 HTTP 错误映射；
- 内部异常文本不复制服务商敏感错误消息；
- 分类节点在模型故障时返回完整人工状态；
- 完整状态图继续执行人工响应节点；
- 模型故障后不调用订单工具；
- 原有 mock、真实模型、订单权限和 API 测试不回归。

## 面试追问

- 为什么不在每个 LangGraph 节点中直接捕获供应商 SDK 异常？
- 为什么模型失败后转人工，而不是自动切回关键词规则？
- `retryable=true` 为什么不代表应该立即重试？
- HTTP 200 的业务降级与 readiness/告警分别解决什么问题？
- 如何确保日志既能排错，又不会泄露 API Key 和用户数据？
- 如果未来加入重试，如何控制幂等性、指数退避、抖动、总耗时和 Token 成本？
