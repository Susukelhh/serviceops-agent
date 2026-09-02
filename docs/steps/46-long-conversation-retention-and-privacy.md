# 第46步：长会话窗口、TTL清理与隐私删除

## 这一步解决什么问题

第45步已经能从可信轮次重建结构化记忆，但还有三类生产问题没有闭环：

1. 会话持续几十轮后，不能把全部原文和模型答案不断塞回模型；
2. `expires_at` 只阻止过期会话继续使用，并不代表数据库和 LangGraph Checkpoint 已经物理清除；
3. 用户删除会话时，业务表与 Checkpointer 不在同一个数据库事务中，删除顺序错误会留下孤儿数据或绕过生命周期检查。

第46步把“短窗口上下文、确定性安全摘要、到期清理、用户隐私删除”组合成一套可重试的生命周期治理。

## 长会话不是无限拼接历史

在线追问解析器仍只读取最近的有限轮次；结构化记忆则从仓库最近50轮事实源重建。超过配置阈值后，系统生成一个有界窗口摘要，
但它不是把用户原话交给另一个LLM自由总结，而是一个确定性投影。

摘要只允许读取以下字段：

```text
sequence_number
status = completed
intent
verified_order_ids
cited_document_ids
```

它明确不读取：

- 用户原消息；
- 独立问题文本；
- 模型答案；
- 退货原因和审批备注；
- 工具原始响应；
- 检索切片正文。

因此手机号、地址、提示注入和模型幻觉不会被摘要再次固化。摘要只表达“最近窗口完成了多少轮、各意图出现几次、验证过哪些订单、
引用来源共有多少个”这类元数据。文档ID虽然来自结构化字段，但命名本身仍可能带邮箱或手机号，所以摘要只计数、不拼接原始文档ID。

`waiting_approval` 可以参与当前焦点记忆，让“这个退货申请”仍能正确指代；但它是可变状态，不进入长期摘要。
只有不可变的 `completed` 轮次能进入摘要，`failed`、普通 `running` 和审批认领中的 `running` 都不能进入。

## 摘要不是新的事实源

`bounded_summary` 只能帮助解释长会话的大致结构，不能用于：

- 选择订单号或调用工具；
- 判断订单归属和权限；
- 恢复审批；
- 证明某个知识答案有充分证据；
- 绕过下一轮检索和 Evidence Sufficiency 检查。

当前追问改写器也不会读取 `bounded_summary`；它仍只信当前显式输入、最近有限轮次和经过工具验证的结构化槽位。
先保存摘要是为了建立稳定、可审计的长窗口投影，不提前把它升级成模型的可信上下文。

订单参数仍只能来自当前显式输入或 `verified_order_ids`；知识答案仍需重新检索当前发布版本。

摘要按完整字段和完整订单ID逐项加入字符预算，放不下的条目整体丢弃，不会把 `SO100001` 截成半个。
窗口末端序号保存在 `summary_window_end_sequence`；它表示“本次窗口看到的最后一轮”，不是从第1轮开始连续覆盖的累计水位。

同一组轮次以任何HTTP完成顺序重建，都会按 `sequence_number` 得到相同摘要；相同事实源重放也不会增加 `memory_version`。

## TTL为什么分为逻辑失效和物理清理

一到 `expires_at` 就在用户请求里同步删除全部数据，会把慢I/O和跨存储失败暴露给正常读写。因此生命周期分两层：

```text
逻辑层：ACTIVE -> EXPIRED/CLOSED，立即阻止新轮次、读取和审批恢复
物理层：删除全部Checkpoint成功后，再删除conversation_turns和conversations
```

创建新轮次时必须检查真实当前时间，所以过期会话不能通过幂等重放继续运行。但已经在TTL前成功接纳的轮次允许跨过到期瞬间完成；
否则图已经执行完，仓库却拒绝落终态，会留下永久 `running`。清理器遇到 `accepted/running` 会跳过，等它终态化后再处理。

这里的TTL是“停止接纳新请求并进入清理候选”的逻辑期限，不是无条件物理擦除SLA。如果worker在 `accepted/running` 后永久崩溃，
本步选择失败关闭，不会仅凭时间删除仍可能恢复执行的Checkpoint；该会话会隔离并超过TTL保留，直到受审计的工作流恢复/终止流程处理。
生产上若要承诺硬物理保留上限，必须先实现带心跳与fencing的执行租约或人工确认终止通道，不能直接强删。

清理批次使用显式带时区的 `now` 和1到1000之间的 `limit`，并处理三类记录：

- `active AND expires_at <= now`：标记为 `expired`；
- 上次Checkpointer清理失败而遗留的 `expired`；
- 用户删除时已标记 `closed`、但跨存储清理尚未成功的补偿记录。

PostgreSQL用行锁和 `SKIP LOCKED` 避免多个清理器互相阻塞；SQLite用 `BEGIN IMMEDIATE`；内存实现用同一把锁。

## 为什么删除必须分两阶段

会话轮次表保存 `workflow_thread_id`，它是找到并删除 LangGraph Checkpoint 的唯一映射。正确顺序是：

```text
ACTIVE
  -> 原子标记 CLOSED 或 EXPIRED
  -> 保留并读取全部 workflow_thread_id
  -> 对每个线程调用 Checkpointer.adelete_thread
  -> 所有Checkpoint都删除成功
  -> 再在业务仓库事务中删除全部轮次
  -> 最后删除会话
```

绝不能先删 `conversation_turns`。如果先丢映射、随后Checkpoint删除失败，残留线程在审批API中可能被误判为旧版单轮 `/chat` 线程，
从而失去会话所有权、关闭状态和TTL边界。

官方 Saver 的线程删除是幂等操作。如果第3个线程失败，前两个已经删除也没关系：会话保持不可见的 `closed/expired`，轮次映射全部保留，
下次重试再次删除前两个，然后继续第三个。只有全部成功后才允许业务表物理删除。

物理删除前，仓库还会重新核对：

- 会话所有者和准备状态没有变化；
- 实际线程ID集合与删除计划完全相同；
- 没有新出现的 `accepted/running` 轮次。

这样伪造、遗漏或过期的删除计划不能造成Checkpoint漏删。

## 用户删除API的不可枚举语义

用户删除自己的会话使用：

```text
DELETE /api/v1/conversations/{conversation_id}
```

JWT `sub` 仍是唯一所有者来源，权限仍是 `agent:chat`。以下情况统一返回 `204 No Content`：

- 自己的会话删除成功；
- 随机UUID不存在；
- UUID属于其他用户；
- 相同删除请求再次重试。

这样DELETE响应不会变成探测其他用户会话ID的旁路。自己的会话若仍有 `accepted/running` 工作流，固定返回409；Checkpoint存储暂时失败则返回503，
但会话已经逻辑关闭且映射保留，用户可安全重试。

## TTL清理API与最小权限

内部清理入口使用独立运维Scope，而不是复用Outbox对账权限：

```text
POST /api/v1/internal/conversations/cleanup
operations:conversation-cleanup
```

它只返回本批领取计划、成功删除和失败保留的聚合数量；被活动轮次暂缓的会话不会被本批领取。
响应不包含用户ID、会话ID、消息或线程ID，普通客户Token不能调用该入口。
生产环境可由定时任务调用同一应用服务；接口本身不代替任务调度器。

每次领取计划都会刷新 `updated_at` 并把失败项轮转到候选队尾，因此达到 `limit` 的“毒计划”不会永久饿死后来到期的会话。
本步还没有跨实例清理租约、失败次数和 `next_retry_at`；官方线程删除与业务终结都是幂等的，所以重复领取是安全的，但生产调度仍应限制并发和频率。

## 删除边界：聊天记忆不等于全部业务事实

本接口删除：

- 会话结构化记忆和安全摘要；
- 用户消息、独立问题和助手答案；
- 会话轮次与工作流线程映射；
- 各轮 LangGraph Checkpoint、writes和blobs。

它不会联动删除：

- 已经提交的退货业务记录；
- Transactional Outbox事实；
- 审批追加式审计链；
- 与会话无归属关系的旧 `/api/v1/chat` 线程。

这些记录有独立的业务或合规保留依据。在线204也不能声称数据库备份、外部Trace和第三方模型服务中的副本已经同步擦除；
它们必须通过各自的保留期、恢复后重删和供应商数据政策治理。

## 审批超时为什么仍不自动抢锁

第45步的 `waiting_approval -> running` CAS能阻止并发双resume，但进程在认领后崩溃时仍采用失败关闭。
本步没有把 `updated_at` 伪装成审批租约：它混合了普通状态变化，无法证明旧worker已经停止。仅凭“超过N秒”把轮次改回等待态，会允许旧worker和新worker同时resume。

安全的自动恢复需要独立租约记录、claim token、fence generation、心跳、稳定审计决定ID，并在终态更新时校验token和代次；
即便如此，跨崩溃resume仍是至少一次语义，业务写必须继续依靠幂等键保证最多一条记录。第46步保持诚实的失败关闭边界，
不宣称 `Command.resume` exactly-once，也不引入不安全的超时自动解锁。

## 自动测试覆盖

- 摘要阈值、50轮窗口和确定性顺序；
- 用户原文、模型答案、手机号、提示注入和可能含PII的文档ID不会进入摘要；
- `waiting_approval`保留当前焦点但不进入长期摘要；
- 字符预算不会截断订单号；
- 内存与SQLite两阶段删除契约一致；
- 运行中会话拒绝用户删除、到期清理暂缓；
- `closed/expired`失败计划可重复领取；
- 失败计划轮转到队尾，不会阻断后续到期会话；
- 伪造或遗漏线程ID的计划不能物理删除；
- 到期前已接纳轮次可以跨TTL完成；
- 删除自己的会话、越权不可枚举、幂等重试；
- 两个并发DELETE都得到幂等成功；
- Checkpoint删除成功、失败保留映射和后续补偿；
- 内部清理权限和聚合计数；
- PostgreSQL行锁清理路径；
- 原有单轮聊天、审批、RAG、Outbox和审计测试继续通过。

## 本地验收结果

```text
Ruff:  all checks passed
Mypy: success, 97 source files
Pytest: 351 passed, 4 skipped
```

其中真实PostgreSQL测试在没有配置测试DSN时按设计跳过；代码仍保留独立集成测试入口，不能用SQLite结果冒充多实例数据库验证。

## 本步文件

- 安全窗口摘要：`src/serviceops_agent/application/conversation_memory.py`
- 会话、摘要水位与删除计划契约：`src/serviceops_agent/domain/conversation.py`
- 三种后端生命周期事务：`src/serviceops_agent/infrastructure/conversation_repository.py`
- Checkpoint删除依赖装配：`src/serviceops_agent/infrastructure/runtime.py`
- 用户删除与内部清理接口：`src/serviceops_agent/api/app.py`
- 清理响应Schema：`src/serviceops_agent/api/schemas.py`
- 最小权限策略：`src/serviceops_agent/security/models.py`
- 可调TTL和摘要预算示例：`.env.example`
- 摘要测试：`tests/unit/test_conversation_memory.py`
- 生命周期仓库测试：`tests/unit/test_conversation_repository.py`
- 会话API测试：`tests/api/test_conversation_api.py`
- PostgreSQL集成测试：`tests/integration/test_postgres_conversation_repository.py`
