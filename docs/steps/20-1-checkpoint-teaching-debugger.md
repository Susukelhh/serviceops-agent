# 第 20.1 步：可单步回放的 Checkpoint 教学调试器

## 这一步解决什么问题

第 20 步时间线展示的是节点主动追加的 `events`。它适合快速演示，但不能回答更深入的问题：

- 每个节点执行后，`ServiceState` 到底新增或修改了哪些字段？
- LangGraph 在什么时候创建 Checkpoint？
- 条件边选择了哪个下一节点？
- RAG 候选、工具计划、工具观察和人工中断分别出现在哪个快照？
- `Command.resume` 后的快照如何接在暂停点后面？

第 20.1 步新增“教学调试模式”。它直接读取 LangGraph 官方 `aget_state_history` 返回的
`StateSnapshot`，再把快照按最早到最晚排列，计算相邻状态差异并交给前端单步播放。

## 生活类比

把一次 Agent 执行想成工单经过多个柜台：

```text
柜台                              LangGraph
当前工单上的全部信息               State
柜台处理完后拍下的一张照片          Checkpoint / StateSnapshot
下一张照片和上一张照片的不同        state_changes
照片背面写着“接下来去哪个柜台”      next
暂停并等待主管签字                  interrupt
签完字后从原照片继续                Command.resume
```

Checkpoint 不是“数据库每改一个字段就拍照”。LangGraph 在每个 super-step 边界保存整份状态快照。
顺序图中通常可以近似理解为“一个节点完成后保存一次”。并行 fan-out 时，同一 super-step 可以同时
完成多个节点，因此面试时不要把 Checkpoint 永远说成“一节点一快照”。

## 后端数据流

```text
GET /api/v1/debug/threads/{thread_id}
  ↓ developer JWT：debug:read
  ↓ production 环境直接 404
  ↓ graph.aget_state_history(config, limit=201)
  ↓ 最新优先历史反转为时间正序
  ↓ State 字段白名单 + 嵌套敏感键过滤
  ↓ 相邻安全状态计算 added / updated / removed
  ↓ 节点中文说明 + 条件边结构化说明
  ↓ ThreadDebugResponse
  ↓ 浏览器 Checkpoint 播放器
```

核心转换逻辑位于 `src/serviceops_agent/api/debug_trace.py`。API 没有直接查询 PostgreSQL 的
`checkpoints` 或 `checkpoint_blobs` 表，因为那些表属于 LangGraph Checkpointer 的内部实现。
读取图的官方状态接口可以降低对数据库私有表结构的耦合。

## 页面能看到什么

每个 Checkpoint 支持五个视角：

| 视角 | 内容 |
|---|---|
| 状态变化 | 与上一个快照相比，哪些公开字段新增、更新或移除 |
| 当前状态 | 当前快照中全部允许教学查看的 State 字段 |
| 工具 / RAG | 工具计划、调用次数、结构化结果、检索候选、分数与引用 |
| 安全审批 | 是否转人工、是否缺参、退货草案、审批结论与流程阶段 |
| Checkpoint | checkpoint_id、父快照、step、source、创建时间和 next |

页面还显示“刚完成的节点”和“下一节点”。顺序图中，当前快照刚完成的节点来自上一快照的 `next`；
这比让前端根据事件名称猜节点更可靠。

## 为什么仍不展示隐藏推理原文

模型的逐字隐藏推理不等于系统执行轨迹。本项目展示的是可以验证的工程事实：

- 模型或确定性分类器的结构化输出：`intent`、`intent_confidence`、`route_reason`；
- 规划器的有限 `ToolCallPlan`；
- 真实工具名、受控参数和经过 Schema 校验的结果；
- RAG 候选、分数、证据门和引用；
- 条件边最终选中的节点；
- Checkpoint 标识、状态差异、interrupt 与恢复结果。

接口固定返回 `hidden_reasoning_exposed=false`。条件边说明由后端根据公开状态和真实 `next` 生成，
不会要求模型补写一段看似合理但不可验证的“思考过程”。

## 安全边界

调试接口使用字段白名单。下列信息不会进入响应：

- JWT 或 Token `jti`；
- `user_id`、审批人 ID 和审批备注；
- `idempotency_key`；
- 工具去重指纹；
- Outbox 内部事件编号；
- 未知 Python 对象的 `repr` 或异常正文。

退货草案只展示动作、订单、原因和风险级别；审批决定只展示 `approved`。嵌套字典还会再次按敏感键
过滤，防止未来某个工具结果夹带同名字段。

新增的 `developer` 角色只有 `debug:read`，不能对话、审批、读取审计链或触发运维补偿。调试路由在
`production` 运行时固定返回 404，并且不会进入生产 OpenAPI。

Checkpointer 的 `JsonPlusSerializer` 也改为显式允许项目真正需要恢复的六个领域类型，未开启
`pickle_fallback`，避免为了读取历史而放开任意 Python 类型反序列化。

## 如何验证

1. 重建并启动 Docker：

   ```powershell
   docker compose up --detach --build --wait --wait-timeout 120
   ```

2. 在 PyCharm 运行 `examples/09_generate_dev_tokens.py`，复制 customer 和 developer Token。

3. 打开 `http://127.0.0.1:8000/console/`，在“身份与权限”分别粘贴两枚 Token。

4. 运行“本人订单工具”场景。页面会自动读取回放；点击 `C1、C2……` 或左右箭头逐步查看。

5. 选择“状态变化”，找到 `intent`、`planned_tool_call`、`tool_result` 和 `answer` 首次出现的步骤。

6. 再运行退货场景。在最后一个橙色 `I` 步骤确认 `interrupt` 存在、写工具尚未执行；批准后播放器
   会重新读取历史，并在原暂停点之后出现审批结论和写工具快照。

也可以运行自动冒烟：

```powershell
uv run python examples/20_agent_console_smoke_test.py
```

它会验证页面、CSP、四项 readiness、本人订单工具、至少八个真实 Checkpoint、
`execute_order_tool` 节点和敏感字段不泄漏；不会调用千问或创建退货。

## 面试官可能追问

### 为什么不直接查 Checkpoint 数据库表？

数据库表是 Checkpointer 的内部存储格式，版本升级可能变化。应用层通过
`aget_state_history` 获取稳定的 `StateSnapshot`，只依赖 LangGraph 公共接口。

### 你如何知道刚执行了哪个节点？

历史按时间正序后，上一快照的 `next` 就是从上一快照继续时要执行的节点。当前快照与上一快照的
状态差异对应这些节点产生的公开写入。并行图要保留节点列表，而不是假设永远只有一个。

### 这是不是 LangSmith 的替代品？

不是。当前面板用于本地教学和项目演示，只展示项目定义的脱敏状态。生产级跨服务 Trace、模型调用
成本、数据集评测和团队协作仍应使用 OpenTelemetry/LangSmith 等专业平台，并设置访问控制和保留策略。

## 主要文件

- `src/serviceops_agent/api/debug_trace.py`：快照转换、字段白名单、差异和条件边说明；
- `src/serviceops_agent/api/schemas.py`：调试 API 的强类型响应；
- `src/serviceops_agent/api/app.py`：`debug:read` 路由和 production 关闭门；
- `src/serviceops_agent/infrastructure/checkpoint_serde.py`：Checkpoint 类型反序列化白名单；
- `src/serviceops_agent/web/index.html`：教学播放器结构；
- `src/serviceops_agent/web/assets/console.js`：单步回放与五种视图；
- `tests/api/test_debug_trace.py`：权限、脱敏、interrupt 和环境边界测试。

## 一手资料

- [LangGraph Persistence 官方文档](https://docs.langchain.com/oss/python/langgraph/persistence)：
  `StateSnapshot`、`get_state_history`、thread、super-step、interrupt 和 time travel 的当前权威说明。
