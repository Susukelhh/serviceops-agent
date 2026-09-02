# 第43步：开放会话API并守住幂等、所有权与并发边界

## 这一步解决什么问题

第41、42步已经有领域契约和持久化仓库，但客户端还只能调用原来的单轮 `/api/v1/chat`。
第43步把会话能力接入 FastAPI 运行时，形成可以真实调用的三组接口：

```text
POST /api/v1/conversations
GET  /api/v1/conversations/{conversation_id}
POST /api/v1/conversations/{conversation_id}/messages
```

原 `/api/v1/chat` 保持兼容，没有被删除或偷偷改变语义。

## 三个ID为什么不能合并

- `conversation_id` 表示一段多轮对话，受用户所有权和TTL保护；
- `turn_id` 表示其中一条消息，承载轮次序号、状态和幂等结果；
- `workflow_thread_id` 只表示这一轮 LangGraph 执行，用于 Checkpoint 和人工审批恢复。

如果整段会话共用一个 LangGraph `thread_id`，第二个问题可能从上一轮终点继续，审批中断也容易与后续问题互相污染。
现在每轮使用独立工作流线程，同时由会话表把多轮组织在一起。

## API流程

创建会话时，所有者只能来自已验签JWT的 `sub`，默认TTL为7天，可通过
`SERVICEOPS_CONVERSATION_TTL_DAYS` 在1至30天范围内配置。

提交消息时执行以下顺序：

1. 以 `conversation_id + owner_user_id` 校验会话；
2. 用客户端必填的 `idempotency_key` 原子创建或读取轮次；
3. 把该轮从 `accepted` 原子推进到 `running`；
4. 使用轮次自己的 `workflow_thread_id` 执行现有 LangGraph；
5. 普通终点保存为 `completed`，审批中断保存为 `waiting_approval`，异常保存为 `failed`；
6. 只把答案、意图、已验证订单号和引用文档号写入会话索引。

这一阶段的 `standalone_question` 暂时等于原始消息。真正结合历史解析“它、那个订单、上一个政策”等追问，是第44步的职责。

## 幂等重放怎么工作

同一个幂等键配同一条消息时：

- 已完成或等待审批：从原 `workflow_thread_id` 的 Checkpoint 构造原结果，不再次运行模型、检索或工具；
- 正在运行：返回409，防止并发重复执行；
- 已失败：返回409，调用方必须使用新幂等键明确发起新尝试；
- 尚未被接管：允许当前请求原子接管。

相同幂等键若配了不同消息会返回409，避免客户端误把旧结果当成新问题的回答。

若审批接口已经恢复了一轮工作流，但会话索引仍是 `waiting_approval`，后续幂等重放会从最新 Checkpoint 识别完成结果并修复索引状态。
第45步把审批恢复与会话索引更新进一步整合为直接同步。

## 所有权和隐私边界

会话所有者只从JWT获得，请求体禁止提交 `user_id`。不存在、属于他人、关闭或过期的会话统一返回
`404 未找到可用会话`，调用方不能根据响应差异枚举别人的会话ID。

会话详情最多返回最近20轮，并且只公开用户消息、答案、有限状态和三个定位ID；不会返回幂等键、内部结构化记忆、审批备注或完整Checkpoint。

## 运行时装配

- memory模式装配进程内会话仓库；
- SQLite模式把会话表放在业务数据库中；
- PostgreSQL模式复用受限连接池和Alembic创建的表；
- LangGraph Checkpointer继续使用自己的存储和生命周期。

因此开发测试、单机学习和多实例部署使用相同API语义。

## 自动测试

新增API测试验证：

- 创建会话并执行两轮，序号为1、2且工作流线程不同；
- 相同消息和幂等键重放得到同一 `turn_id`、`thread_id` 和答案；
- 同键不同消息返回409；
- 运行中轮次的并发重试返回409而不重复执行；
- 其他用户读取和发送都得到相同404；
- 缺失幂等键或伪造 `user_id` 在HTTP边界返回422。

## 调用示例

先创建会话：

```powershell
$conversation = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/conversations `
  -Headers @{ Authorization = "Bearer $token" }
```

再提交一轮；网络重试时必须复用同一个幂等键：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/v1/conversations/$($conversation.conversation_id)/messages" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" `
  -Body '{"message":"查询订单 SO100001","idempotency_key":"client-turn-0001"}'
```

## 本步文件

- API路由与执行协调：`src/serviceops_agent/api/app.py`
- HTTP Schema：`src/serviceops_agent/api/schemas.py`
- 三后端运行时装配：`src/serviceops_agent/infrastructure/runtime.py`
- TTL配置：`src/serviceops_agent/config/settings.py`
- API测试：`tests/api/test_conversation_api.py`
