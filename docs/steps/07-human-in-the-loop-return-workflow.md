# 第七步：可恢复人工审批与幂等退货写操作

> 本文记录第七步当时的实现。第九步已经把请求体 `user_id/reviewer_id` 替换为 JWT `sub`，
> 当前调用方法以 README 和第九步文档为准。

## 本步完成了什么

第六步的订单 Tool 全部是只读操作。本步首次加入会改变业务状态的操作：为本人已签收订单创建
退货申请。写操作没有被塞进普通 ReAct 循环，而是使用显式的 Human-in-the-loop 子图：

```text
prepare_return_request（只读预检查 + 强类型草案）
→ request_return_approval（interrupt，保存 Checkpoint）
→ 外部审批接口提交 Command(resume=...)
→ approved ? execute_return_request : finalize_return_rejection
→ create_return_request Tool（身份绑定 + 幂等写入）
```

首次图调用停在 `interrupt`，不会执行后面的条件边和写工具。审批接口使用原 `thread_id` 恢复
同一份 State，只有 `approved=True` 才能到达 `execute_return_request`。

## 为什么退货申请不直接交给 Agent 自主执行

“查询订单”和“创建退货申请”风险不同。模型可以建议下一步，但不能因为生成了一段合法 JSON 就
自动获得业务写权限。本项目把控制权分成四层：

1. 分类器只决定进入退货子图；
2. 草案节点确定性提取用户明确提供的订单号和原因，并做只读资格预检查；
3. 人工审批只批准或拒绝已经冻结的草案；
4. 写执行节点复查批准状态，再创建身份绑定工具。

这使面试时可以精确回答“谁拥有决策权、谁拥有执行权、失败时在哪里停止”。

## interrupt 与 Command.resume 的关键语义

`interrupt(payload)` 首次运行时会暂停当前节点，把 State 和下一执行位置交给 Checkpointer 保存。
外部系统随后用同一 `thread_id` 调用：

```python
await graph.ainvoke(
    Command(resume={"approved": True, "reviewer_id": "...", "comment": "..."}),
    config={"configurable": {"thread_id": "原线程"}},
)
```

恢复时，中断节点会从函数开头重新执行，直到同一个 `interrupt` 位置取得 resume 值。因此有一条
非常重要的规则：中断节点在 `interrupt()` 之前不能执行不可幂等副作用。本项目在该位置之前只
构造审批展示负载，真正写入被放在独立的后续节点。

可持久化 State 中的意图、退货草案、流程状态和审批决定使用 JSON 字符串/字典保存，不直接保存
自定义 Pydantic 或枚举实例。每个节点读取时再通过领域 Schema 重建强类型对象。这样避免
Checkpointer 依赖任意 Python 类反序列化，也更便于未来迁移到数据库或跨版本部署。

## Checkpointer 与 thread_id

启用 Checkpointer 的编译图每次调用都必须提供：

```python
{"configurable": {"thread_id": "稳定线程标识"}}
```

API 首次请求生成 `thread_id` 并返回客户端，审批接口从路径参数获取它。恢复前 API 会读取快照：

- 线程不存在：404；
- 已经完成或不在指定审批节点：409；
- 确实停在退货审批中断：允许一次恢复。

当前使用 `InMemorySaver`，适合 PyCharm、本地学习和单进程测试。进程重启后快照会丢失，多实例
也不能共享状态，因此不能把它描述成生产级持久化。生产环境要替换成数据库 Checkpointer，
同时配置备份、过期清理、加密和多实例一致性。

## 审批负载为何不包含 user_id 和幂等键

`ApprovalRequestPayload` 只暴露：

- 固定动作 `create_return_request`；
- 经过归属预检查的订单号；
- 用户明确提供的原因；
- `write` 风险标签；
- 固定的操作效果说明。

可信 `user_id`、原始消息和 `idempotency_key` 保存在 Checkpoint State 中，不允许审批请求重新
提交。否则攻击者可能在 resume 时把批准 A 用户订单的决定替换成 B 用户身份或另一组工具参数。

当前 `reviewer_id` 为了演示由请求体提交；生产中也必须从经过 JWT/RBAC 验证的审批人身份上下文
注入，并检查该角色是否有退货审批权限。

## 幂等的三个层次

### 请求级稳定键

`ChatRequest.idempotency_key` 允许调用方在同一业务写请求重试时复用稳定键。调用方不提供时，
API 使用本次 `request_id`，只能保证该线程内唯一。

### 仓库原子检查

`InMemoryReturnRequestRepository` 在锁内完成“检查键—比较负载—创建记录”：

- 同键 + 同用户 + 同订单 + 同原因：返回原记录，标记 `idempotent_replay=True`；
- 同键 + 不同负载：返回 `idempotency_conflict`，不覆盖旧记录；
- 新键：创建唯一 `RR-...` 申请。

### API 重复恢复门

一个线程审批完成后就不再包含 interrupt。再次调用同一审批 URL 会返回 409，避免把已经完成的
Checkpoint 当成新任务执行。

进程内锁仍不等于生产级幂等。生产数据库需要唯一索引、事务和冲突读取策略，才能在多进程、
崩溃重启和网络重试下保证 exactly-once effect（业务效果至多一次）。

## 写工具的纵深防御

即使条件边已经检查批准状态，`execute_return_request` 仍会复查：

- `return_request_proposal` 必须是强类型草案；
- `approval_decision` 必须是强类型且 `approved=True`；
- 流程状态必须为 `APPROVED`；
- `user_id` 必须来自可信 State；
- Tool 参数必须只取自已经批准的草案；
- Tool 返回必须通过 `ReturnRequestResult` 一致性校验。

工具 Schema 没有 `user_id`。仓库在实际写入边界再次检查订单归属和 `DELIVERED` 状态，避免审批
等待期间订单状态变化导致非法写入。

## 在 PyCharm 中观察

右键运行：

```text
examples/07_human_approval_return.py
```

四个场景的预期结果：

1. interrupt 返回安全审批负载，仓库记录数为 0；
2. 明确批准后才出现 `RR-...`，记录数变为 1；
3. 拒绝后记录数不变；
4. 新线程复用相同幂等键和相同负载，返回原编号，记录数仍为 1。

该示例强制使用本地分类、Hash Embedding、内存 Qdrant 和确定性生成，不会读取 `.env` 中的真实
千问开关，也不会产生 Token 费用。

## 在 Swagger 中体验

先启动 API：

```powershell
uv run uvicorn serviceops_agent.api.app:app --reload
```

打开 `http://127.0.0.1:8000/docs`：

1. 调用 `POST /api/v1/chat`，提交 `user-001`、包含 `SO100002` 和明确原因的退货申请；
2. 从响应复制 `thread_id`，确认 `approval_required=true` 且没有 `return_request_id`；
3. 调用 `POST /api/v1/approvals/{thread_id}`，提交批准或拒绝；
4. 批准时应得到 `return_workflow_status=completed` 和 `RR-...`；
5. 对同一线程再次审批应得到 409。

## 本步测试

当前共 75 个测试，新增重点包括：

- 写 Tool Schema 不包含 `user_id`；
- 本人已签收订单首次创建成功；
- 同键同负载返回原编号且仓库只有一条；
- 同键不同负载安全冲突；
- 越权订单不泄漏存在性，未签收订单不允许写入；
- 图首次 interrupt 前仓库为零；
- 批准后才执行写 Tool；
- 拒绝路径没有工具名、申请编号或仓库新增；
- 两个线程使用同一幂等键只创建一次；
- API 审批负载不泄漏身份/幂等键；
- `approved` 必须是真实 JSON 布尔值，字符串 `"true"` 在 API 边界返回 422；
- 已完成线程重复审批返回 409；
- 原有分类、RAG、工具循环和故障降级测试全部不回归。

## 面试追问与回答方向

- 为什么 interrupt 节点前不能写数据库？
  恢复时节点会从头重放，interrupt 前的非幂等副作用可能重复执行。
- Checkpointer 和业务数据库分别保存什么？
  前者保存工作流 State/执行位置，后者保存真实业务申请；二者不能互相替代。
- 有 thread_id 为什么还需要 idempotency_key？
  thread_id 保证恢复同一工作流；客户端可能创建新线程重试，业务幂等键保证跨线程不重复写。
- 人工批准是否等于信任所有 resume 参数？
  不是。resume 只能提交结构化决定，身份和工具参数来自批准前冻结的可信 State。
- 如何应对审批期间订单状态变化？
  草案阶段做预检查提升体验，写仓库在提交瞬间再次检查资格，避免 TOCTOU 问题。
- 当前方案哪里还不生产级？
  InMemorySaver/仓库无法跨进程持久化，请求体 user_id/reviewer_id 未鉴权，也尚缺审计表、指标和告警。
