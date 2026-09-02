# 第 61 步：SSE 多轮会话与受控真实模型演示

## 这一阶段解决什么

原控制台调用单轮 `/api/v1/chat`，浏览器只能在整个图结束后看到响应。现在控制台先创建服务端
会话，再向 `/api/v1/conversations/{conversation_id}/messages/stream` 发起带 JWT 和幂等键的
POST 请求。服务端按顺序发送：

- `accepted`：请求已经通过认证并占用执行工位；
- `progress`：工作流仍在执行，长模型请求每五秒发送一次；
- `result`：完整 `ConversationMessageResponse`，与同步接口保持一个领域契约；
- `error`：流开始后的有限业务错误，不包含调用栈、密钥或模型原始异常。

这不是伪造的逐字 Token 流。当前 LangGraph 节点仍以完整结构化输出为提交边界，SSE 只暴露真实
工作流生命周期。Nginx 对该路径关闭缓冲和重试；客户端断线后取消本地任务，重试必须复用同一个
幂等键。

## 默认零费用验收

```powershell
docker compose up --build --detach --wait --wait-timeout 120
```

默认仍是 `mock + deterministic + hash + extractive`，不会读取模型密钥。打开
`http://127.0.0.1:8000/`，连续提问两次即可验证同一 `conversation_id` 的多轮上下文与 SSE 状态。

## 显式启用真实模型

```powershell
Copy-Item .env.real-model.example .env.real-model
# 在 .env.real-model 中填写真实 Key、模型和经过评测的阈值
docker compose -f compose.yaml -f compose.real-model.yaml up --build --detach --wait --wait-timeout 180
uv run python examples/09_generate_dev_tokens.py
```

把 customer/reviewer/auditor/developer 短期 Token 粘贴到控制台“身份与权限”面板后再演示。该覆盖档
有意关闭匿名会话，将每个 Agent 的模型并发收紧到 2，并使用独立 Qdrant Collection；它不会把
密钥写入镜像、仓库或命令行。不要把 `docker compose config` 的展开结果粘贴到公开日志，因为
`env_file` 内容可能出现在输出里。

停止服务但保留数据库和向量卷：

```powershell
docker compose -f compose.yaml -f compose.real-model.yaml down
```

真实模型只说明“链路可以运行”，不等于候选版本可发布。发布结论仍必须来自固定数据集、独立
裁判、费用/延迟记录和回归门禁。
