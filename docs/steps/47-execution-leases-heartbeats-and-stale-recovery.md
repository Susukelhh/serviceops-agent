# 第47步：执行租约、心跳与僵死轮次恢复

## 这一步解决什么问题

第46步已经能阻止清理器删除真正运行中的工作流，但 `accepted/running` 当时只有状态，没有“谁在执行、执行权到什么时候、旧worker是否还能写终态”的信息。
如果进程在状态更新后崩溃，轮次会永久阻塞用户删除和TTL清理。

第47步不再把 `updated_at` 猜成租约，而是为每个会话轮次建立独立执行记录：

```text
execution_kind
lease_state
claim_token
fence_generation
claimed_at
heartbeat_at
lease_expires_at
decision_audit_event_id（仅审批恢复）
```

`claim_token` 是服务端内部秘密，不进入HTTP响应、日志、Trace、Checkpoint或模型上下文；`fence_generation` 是单调递增的非秘密代次，防止旧执行者晚到覆盖新代次。

## 业务状态和执行权必须分开

轮次状态回答“业务进行到哪一步”：

```text
accepted -> running -> waiting_approval -> completed/failed
```

租约回答“当前哪个worker有权推进它”。两者不能继续混用：

- 首次消息取得 `initial` 租约时，原子执行 `accepted -> running`；
- 审批恢复取得 `approval_resume` 租约时，轮次继续保持 `waiting_approval`；
- 审批不再为了加锁临时伪装成普通 `running`；
- 所有终态写入必须同时匹配 `claim_token + fence_generation + execution_kind`。

租约记录即使完成后也保留为 `released`，下一阶段认领时 generation 继续递增，不能出现旧token在删除再创建后重新有效的ABA问题。

## 心跳如何工作

一次 `graph.ainvoke` 可能包含模型、检索、数据库和工具I/O，不能只在LangGraph节点边界续租。API在图调用作用域内启动心跳循环：

1. 调图前原子取得租约；
2. 每隔固定间隔续期；
3. 正常完成后用同一fence提交终态并释放租约；
4. 首次消息图异常时只能用同一fence标记失败；审批恢复的未知异常保留活动租约，
   由恢复器在到期后隔离对账，不能武断写成失败；
5. 心跳失权或仓库不可确认时取消本地图任务，丢弃其结果；
6. `finally` 必须停止心跳，不能留下后台任务泄漏。

配置要求心跳间隔最多为租期的三分之一，为调度延迟、短暂GC暂停和数据库抖动留出空间。

租约刚过期但还没有被恢复器增代次fence时，原worker仍可提交终态；这样不会因毫秒级边界误杀已经成功的请求。恢复器一旦改变generation或token，旧worker的续租和终态提交都会失败。

## 首次消息和审批超时不能用同一策略

首次消息在进入人工审批前没有不可逆退货写入，因此可以保守自动终止：

```text
长期停留 accepted 且无人持有租约
  -> failed

running(initial) 的租约超过宽限期
  -> generation + 1
  -> token更换
  -> lease_state = revoked
  -> turn = failed
```

后台不会静默重新调用模型或节点。相同幂等键仍返回此前失败，用户必须用新幂等键明确发起新轮次。

审批恢复不同。它可能已经发生以下任一事实：

- 决定审计已落库；
- 退货记录和Transactional Outbox已经原子提交；
- Checkpoint已写终态但会话索引尚未同步；
- 写操作尚未开始。

因此 `approval_resume` 租约过期时只做fence和隔离：

```text
lease_state = reconciliation_required
turn继续保持waiting_approval
禁止自动再次Command.resume
禁止TTL物理删除
```

后续必须根据审批哈希链、退货幂等记录、Outbox和固定Checkpoint做权威对账；不能让operator提交一个新的审批决定，也不能仅凭超时猜测失败。

## 恢复器与最小权限

第47步增加独立运维入口：

```text
POST /api/v1/internal/conversations/recover-stale
operations:workflow-recovery
```

它与 `operations:conversation-cleanup` 分权，只返回低敏聚合计数：扫描数、无人认领的accepted失败数、initial租约失败数、审批隔离数和遗留人工处理数。
响应与日志都不包含用户消息、会话ID、线程ID、审批备注、token或订单内容。

Memory实现用同一把锁，SQLite使用 `BEGIN IMMEDIATE`，PostgreSQL使用数据库时钟、行锁和 `FOR UPDATE SKIP LOCKED`。
“扫描并增代次fence”必须位于同一事务，不能先列出候选、稍后再撤销，否则心跳和恢复器可能同时获胜。
遗留 `running` 项只能进入人工处置；每次计数后会刷新其扫描排序时间，让小批次继续覆盖后续候选，避免一个坏项永久饿死整条恢复队列。

## 与TTL和隐私删除的关系

清理判断从“看到accepted/running就永久忙”升级为同时查看租约：

- 有效活动租约：继续阻止删除；
- 过期initial租约：先由恢复器fence并标失败，随后可以进入两阶段清理；
- 正常 `waiting_approval` 且没有审批执行租约：仍可由用户关闭；
- 活动审批租约：阻止删除；
- `reconciliation_required`：继续隔离，权威对账前不能删。

所以普通僵死轮次不再永久占住软TTL；含糊的审批写入仍选择失败关闭，不用数据擦除掩盖未对账的业务事实。

## 为什么这仍不是完整的跨存储fencing

本步可以保证旧代次不能覆盖会话轮次终态，并让心跳失败的本地任务主动取消；退货业务记录本身继续依靠稳定幂等键和Transactional Outbox保证最多一条。

但会话租约表、官方LangGraph Checkpointer和外部系统不共享一个数据库事务。仅在会话仓库校验generation，还不能从数学上阻止一个长时间暂停的旧进程稍后写入Checkpoint。

要承诺严格硬擦除SLA，还需要：

- Saver的 `put/put_writes/delete` 也校验generation或线程tombstone；
- 退货写事务在同一事务中校验审批fence；
- 删除前增代次并等待在途fenced写结束；
- 对不支持fence的外部写只通过幂等Outbox适配器发送。

因此本步承诺的是“会话终态CAS已fence、普通僵死可治理、审批僵死可检测并隔离”，不宣称LLM、`Command.resume`或Checkpoint exactly-once。

## 自动测试覆盖

- initial和approval租约的强类型/时间线约束；
- 两个并发claim只有一个成功；
- 正确token可续租，错误token或旧generation失败；
- 心跳续期、失权取消和任务清理；
- 旧fence不能完成、失败或覆盖记忆；
- accepted僵死和initial过期自动失败；
- approval过期进入 `reconciliation_required`，绝不自动resume；
- 审批执行未知异常不伪造 `WORKFLOW_FAILED`，到期后进入对账隔离；
- 遗留人工处置候选在小批次之间公平轮转；
- 活动审批租约及隔离态阻止TTL/用户删除；
- SQLite重启后租约、代次和到期时间仍存在；
- PostgreSQL多实例claim/recovery事务集成测试（配置测试DSN时执行）；
- 恢复接口独立Scope与低敏聚合响应；
- 会话消息和审批正常路径改用租约后保持现有响应兼容；
- 原有单轮 `/chat`、RAG、退货幂等、Outbox和审计测试继续通过。

## 本步文件

- 租约领域契约：`src/serviceops_agent/domain/conversation.py`
- 心跳执行器：`src/serviceops_agent/application/conversation_execution.py`
- 三种租约仓库：`src/serviceops_agent/infrastructure/conversation_repository.py`
- PostgreSQL迁移：`src/serviceops_agent/migrations/versions/20260829_0003_execution_leases.py`
- HTTP执行与恢复入口：`src/serviceops_agent/api/app.py`
- 恢复聚合Schema：`src/serviceops_agent/api/schemas.py`
- 独立恢复权限：`src/serviceops_agent/security/models.py`
- 配置示例：`.env.example`
- 领域、仓库、心跳和API测试：`tests/`

## 本步验证结果

2026-08-30完成全库门禁：

```text
Ruff  : All checks passed
Mypy  : Success, 99 source files
Pytest: 392 passed, 5 skipped
```

跳过项是需要外部PostgreSQL测试DSN等可选环境的集成用例；内存、SQLite、HTTP、心跳、权限、恢复分类和并发审批回归均已在本地通过。
