# 第十二步：事务 Outbox、至少一次投递与崩溃补偿

## 本步解决的问题

第十步的审批成功路径依次执行：

```text
写入 return_requests
→ 提交业务事务
→ 写入 workflow_completed 审计事件
```

如果进程在前两步之间退出，退货申请已经存在，但审计链只有“批准决定”，没有“流程完成”。
这不是 LangGraph State 或普通重试能自动解决的问题，而是典型的 **dual write（双写）** 风险。

本步改成：

```text
BEGIN IMMEDIATE
├─ INSERT return_requests
├─ INSERT return_outbox_events(status=pending)
└─ COMMIT

协调器扫描 pending
├─ 幂等 append workflow_completed
└─ mark outbox processed
```

AWS 的 Transactional Outbox 指南同样强调：业务表与 Outbox 表在同一事务更新，独立处理器再投递；
投递可能重复，因此消费者必须幂等：

- [AWS Transactional Outbox Pattern（中文）](https://docs.aws.amazon.com/zh_cn/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)
- [Debezium Outbox Event Router](https://debezium.io/documentation/reference/stable/transformations/outbox-event-router.html)

## 为什么不追求“恰好一次”

存在一个无法用普通本地事务消除的窗口：

```text
审计 append 已成功
→ 进程退出
→ Outbox 还没有 mark processed
```

重启后协调器必须再次投递，所以传输语义是 **at-least-once（至少一次）**。项目通过以下组合得到
业务上的“有效一次”：

1. Outbox `event_id` 由 `thread_id + event_type` 使用 UUID5 稳定生成；
2. 审计事件 ID 也由 `thread_id + audit event type` 稳定生成；
3. 审计仓库发现同 ID 同语义时返回幂等重放；
4. 发现同 ID 不同主体、摘要或结果编号时拒绝并进入失败/死信；
5. 协调器只有在下游确认成功或幂等存在后才标记 `processed`。

面试时不要说“系统实现了物理上的 exactly-once”。更准确的说法是：

> 传输至少一次，消费者幂等，最终获得业务效果上的一次。

## 原子性如何保证

`SQLiteReturnRequestRepository.create_or_get()` 使用一个连接和一个 `BEGIN IMMEDIATE`：

1. 检查业务幂等键；
2. 二次检查订单归属和状态；
3. 插入退货申请；
4. 插入最小化 Outbox 事件；
5. 两项全部成功才 `commit()`；
6. 任一校验、约束或磁盘异常都会 `rollback()`。

测试会在 `return_outbox_events` 上临时创建一个主动 `RAISE(ABORT)` 的触发器。执行顺序已经到达
业务 INSERT，随后 Outbox INSERT 失败；最终断言业务表和 Outbox 表计数都为零。这比只看代码更直接地
证明两项确实共享同一事务。

内存实现使用同一个 `Lock`，并在修改两个字典前先完整构造 Pydantic 事件。它主要服务测试和教学，
生产语义以 SQLite/后续 PostgreSQL 实现为准。

## Outbox 保存哪些字段

`return_outbox_events.payload_json` 保存：

- LangGraph `thread_id`；
- 初始 `request_id`；
- 已验签 JWT `sub` 对应的审批主体；
- JWT `jti`，不保存完整 Token；
- 固定 `approved=true`；
- 审批草案订单号；
- 草案 SHA-256；
- 审批备注 SHA-256；
- 仓库真正生成的退货申请编号。

它不保存：

- 用户完整自然语言；
- 退货原因原文；
- 幂等键原文；
- 审批备注原文；
- Bearer Token；
- API Key；
- 模型 Prompt/Response。

`thread_id` 和 `token_jti` 由 API 放进 `ApprovalDecision` 的系统字段。HTTP 请求体仍然只有
`approved/comment`；Tool Schema 仍然只有 `order_id/reason/idempotency_key`。系统元数据通过工具闭包
传给仓库，模型不能生成或覆盖。

## 状态、退避和死信

Outbox 有三个有限状态：

| 状态 | 含义 | 普通扫描是否读取 |
|---|---|---|
| `pending` | 等待首次投递或退避结束 | 是 |
| `processed` | 下游已新增或幂等确认 | 否 |
| `dead_letter` | 连续失败达到上限 | 否 |

默认最大失败次数为 3，失败后按 2、4、8 秒设置下一次处理时间。错误列只保存
`audit_conflict/dispatch_error` 等有限错误码，不保存可能包含路径、SQL 或敏感数据的异常正文。

死信当前不会自动恢复。真实企业系统应补充带工单号的人工查看、原因修复、重新排队接口和告警规则；
不能简单无限重试，因为永久语义冲突会造成资源浪费和告警噪声。

## API 如何工作

正常批准成功后，API 会同步尽力协调本线程的一条事件，以降低审计查询延迟。若协调暂时失败：

- 退货业务事务已经可靠提交，API 仍返回真实业务结果；
- Outbox 保持 `pending` 并记录失败次数/退避时间；
- 运维主体后续触发补偿；
- 重复投递不会生成第三条审计事件。

新增权限：

| 角色 | Scope | 能力 |
|---|---|---|
| `operator` | `operations:reconcile` | 触发到期 Outbox 补偿 |
| `auditor` | `audit:read` | 读取并验证审计链，不能补偿 |
| `return_reviewer` | `return:approve` | 决定批准/拒绝，不能补偿 |
| `customer` | `agent:chat` | 发起请求，不能补偿 |

内部运维接口：

```text
POST /api/v1/internal/outbox/reconcile?limit=100
```

响应只包含 `scanned/processed/replayed/failed/dead_letter` 五个计数，不暴露事件载荷。

`GET /ready` 现在真实读取 Checkpointer、退货表、Outbox 表和审计表。Outbox 有 pending 积压并不等于
数据库不可用，因此 readiness 只验证查询能力；积压数量/最老年龄应由 Metrics 和告警监控。

## OpenTelemetry

新增指标：

```text
serviceops.outbox.dispatches{outcome=processed|replayed|failed|dead_letter}
```

新增业务 Span：

```text
serviceops.outbox.reconcile
```

指标不包含 `event_id/thread_id/request_id/user_id`。这些唯一标识只允许出现在受控 Trace/日志中，避免
Metrics 高基数爆炸。

## 在 PyCharm 中运行

1. 打开 `D:\serviceops-agent`；
2. 确认解释器为 `D:\serviceops-agent\.venv\Scripts\python.exe`；
3. 打开 `examples/12_transactional_outbox_recovery.py`；
4. 右键编辑器，选择 **Run '12_transactional_outbox_recovery'**。

或者在 PyCharm Terminal 执行：

```powershell
cd D:\serviceops-agent
uv run python examples/12_transactional_outbox_recovery.py
```

预期重点观察：

```text
运行 A：业务记录数 1，pending Outbox 数 1，审计只有 decision
运行 B：processed 数 1，审计变为 decision + completed，哈希链有效
再次协调：scanned 0，审计事件总数仍为 2
```

示例使用临时 SQLite 文件，不调用千问，不消耗 Token，也不修改 `data/runtime`。

## 代码位置

- `src/serviceops_agent/domain/outbox.py`：事件、可信元数据、状态和批次结果；
- `src/serviceops_agent/infrastructure/outbox_repository.py`：协调器依赖协议；
- `src/serviceops_agent/infrastructure/return_repository.py`：业务 + Outbox 原子提交与状态推进；
- `src/serviceops_agent/application/outbox_reconciler.py`：至少一次投递和幂等消费；
- `src/serviceops_agent/graph/nodes/returns.py`：摘要与可信系统元数据装配；
- `src/serviceops_agent/api/app.py`：即时协调、运维接口和 readiness；
- `src/serviceops_agent/security/models.py`：operator 与 `operations:reconcile`；
- `examples/12_transactional_outbox_recovery.py`：离线重启补偿演示；
- `tests/unit/test_return_outbox.py`：原子回滚、崩溃窗口、重启和死信测试。

## 面试官可能追问

### 1. 为什么不能在提交退货单后直接发消息？

数据库提交成功、发消息失败会丢事件；发消息成功、数据库回滚会产生幽灵事件。两者不是同一个本地
事务资源，因此需要 Outbox、CDC 或更复杂的分布式协调策略。

### 2. 为什么下游必须幂等？

下游成功与上游确认之间始终可能崩溃。重启后无法可靠判断上次是否已被下游处理，只能再次投递，
所以消费者要使用稳定事件 ID 做幂等判断。

### 3. 为什么不把 Outbox 事件直接删除？

保留 `processed` 状态便于教学、排障和统计，也能证明业务提交对应的事件存在。生产中可配置归档和
保留期；不能无限保存，否则表和索引会增长。

### 4. 当前 SQLite 实现怎样扩展到多实例？

SQLite 适合本地求职演示和单服务实例。生产 PostgreSQL 可使用短事务认领批次、
`SELECT ... FOR UPDATE SKIP LOCKED`、租约/锁持有者字段，或使用 Debezium CDC 推送到 Kafka。仍要保留
稳定事件 ID、消费者幂等、重试、死信和顺序策略。

### 5. 为什么完成态由 Outbox，拒绝/失败仍直接写审计？

双写风险来自“业务记录 + 第二持久化边界”。拒绝和写入前失败没有创建退货业务记录，只追加一个审计
终态，不存在两份必须原子一致的业务写。若未来拒绝也要同时修改业务表，也应纳入同一 Outbox 事务。

### 6. 这个实现还缺什么生产能力？

- 常驻后台 worker/定时调度，而不是仅请求后即时协调和手工接口；
- 多实例事件认领、租约超时和严格顺序策略；
- pending 数量、最老事件年龄、失败率和死信告警；
- 死信人工重放与审计；
- 数据归档/保留策略；
- PostgreSQL 迁移和数据库迁移工具；
- 若跨服务投递，增加 Kafka/RabbitMQ/SQS 与下游消费幂等表；
- 混沌测试：在提交、append、mark 等位置主动杀进程验证恢复。

## 本步质量门

```powershell
uv run ruff check .
uv run mypy src
uv run pytest -q
```

重点新增覆盖：

- Outbox INSERT 失败时业务 INSERT 一起回滚；
- 正常协调形成两节点有效哈希链；
- 审计 append 成功、Outbox 未标记的崩溃窗口安全重放；
- SQLite 新实例可处理上次进程留下的 pending；
- 连续失败进入 dead letter；
- 只有 operator 可以触发运维补偿；
- Tool Schema 不暴露系统 Outbox 元数据。
