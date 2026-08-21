# 第十步：审批审计证据链与篡改检测

## 本步解决的问题

第九步已经证明“谁有权审批”，但只把审批决定保存在 LangGraph State 中仍不够：Checkpoint 的
主要职责是恢复工作流，不是长期安全审计。企业系统还需要回答：谁、在什么时间、使用哪一枚
访问凭证，对哪个可信草案作出了什么决定，决定最终是否产生业务记录，以及历史字段是否被改写。

本步链路为：

```text
return:approve JWT
→ 读取并校验 Checkpoint 中的冻结草案
→ 保存 approval_decision_recorded（在 Command.resume 之前）
→ 恢复 LangGraph，批准时执行身份绑定写 Tool
→ 保存 workflow_completed / workflow_rejected / workflow_failed
→ 第二条 previous_event_hash 引用第一条 event_hash
→ auditor 使用 audit:read 读取并触发全链重算
```

决定先于执行结果落库很重要。如果服务在两者之间崩溃，审计员能看到“已经有决定但没有结果”的
不完整业务过程，而不是误以为从未有人审批。当前切片没有自动补偿任务，这类线程需要后续
Reconciliation Job 扫描和处理。

## 三种角色的职责分离

| 角色 | Scope | 能做什么 |
|---|---|---|
| `customer` | `agent:chat` | 发起对话和退货草案 |
| `return_reviewer` | `return:approve` | 批准或拒绝，但不能读审计接口 |
| `auditor` | `audit:read` | 读取审计证据，但不能发起或批准 |

审计员查询：

```text
GET /api/v1/audit/approvals/{thread_id}
```

缺少 Token 返回 401；合法用户或审批人越权读取返回 403；没有审计事件的 UUID 返回固定 404。
审计读取本身也会写一条不含 Token 和事件正文的服务日志。

## 审计表保存什么

`approval_audit_events` 的核心字段包括：

- `audit_event_id`：由 thread + event type 生成的稳定 UUID；
- `thread_id` / `request_id`：关联工作流与原请求；
- `actor_id`：来自已验证 JWT `sub`；
- `token_jti`：具体 Token 标识，但不是完整 Bearer Token；
- `approved` / `order_id` / `return_request_id`：决定目标和最终业务结果；
- `proposal_digest`：冻结草案规范 JSON 的 SHA-256；
- `comment_digest`：规范化审批备注的 SHA-256；
- `chain_position` / `previous_event_hash` / `event_hash`：线程内证据链。

以下内容不会复制到审计表：

- 完整 JWT、JWT 签名密钥、千问 API Key；
- 退货原因原文；
- 客户端幂等键；
- 审批备注原文；
- 用户完整自然语言请求。

OWASP 的日志安全建议明确要求不要直接记录访问 Token、密码、会话标识和其他秘密，并建议对
敏感标识执行掩码、哈希或加密，同时保护日志不被未授权读取、修改和删除：
[OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)。

## 哈希链如何工作

第一条事件的 `previous_event_hash` 固定为 64 个零。每条 `event_hash` 都覆盖：

```text
事件全部业务字段
+ audit_event_id
+ chain_position
+ created_at
+ previous_event_hash
```

字段先用 `sort_keys=True` 和紧凑分隔符编码为确定性 UTF-8 JSON，再计算 SHA-256。第二条事件
引用第一条 `event_hash`。修改第一条的审批人、决定、时间或内容摘要会导致第一条重算失败；即使
攻击者只替换第一条保存的哈希，第二条的前驱引用也会失配。

SQLite 仓库还提供：

- `BEGIN IMMEDIATE`：并发追加前取得写锁；
- `UNIQUE(thread_id, event_type)`：同一语义事件最多一次；
- `UNIQUE(thread_id, chain_position)`：同线程位置不重复；
- 参数化 SQL：外部标识不能改变 SQL 结构；
- `UPDATE/DELETE` 触发器：普通应用连接不能覆盖或删除历史；
- WAL：改善本地单写者、多读者并发；
- 读取时 Pydantic 再校验，并重新计算整条链。

相同主体、决定、草案和备注的重试会返回原事件。Token 续期导致 `jti` 改变时仍视为同一业务
重试，审计表保留第一次实际记录的 `jti`；更换审批主体、批准布尔值或业务内容则返回 409。

## 为什么不称为“绝对不可抵赖”

本实现是应用级只追加和可检测篡改，不是密码学意义上的绝对不可抵赖：

- 拥有数据库管理员权限的人可以删除触发器；
- 管理员若重写所有事件并重新计算整条链，单库内部无法证明原历史；
- SQLite 文件可能被整体替换或回滚到旧备份；
- 删除链尾后，如果没有独立外部锚点，纯链结构不能单独证明尾事件曾存在；
- 本地 HS256 JWT 也不是企业长期身份签名方案。

生产增强方向是：把审计事件异步复制到权限隔离的集中日志平台或 WORM/Object Lock 存储；使用
KMS/HSM 管理的非对称密钥对批次头签名；把链头定期锚定到独立系统；配置保留策略、备份、告警和
双人管理审批。NIST SP 800-92 提供了企业日志管理生命周期参考：
[NIST Guide to Computer Security Log Management](https://csrc.nist.gov/pubs/sp/800/92/final)。

## 代码位置

- `src/serviceops_agent/domain/audit.py`：事件模型、规范化摘要和哈希计算；
- `src/serviceops_agent/infrastructure/audit_repository.py`：内存/SQLite 追加、幂等和校验；
- `src/serviceops_agent/api/app.py`：决定前置记录、结果追加和审计查询；
- `src/serviceops_agent/security/models.py`：auditor 与 `audit:read`；
- `examples/10_approval_audit_chain.py`：完全离线的两节点哈希链演示；
- `tests/unit/test_approval_audit_repository.py`：触发器、重启和管理员篡改测试。

## 在 PyCharm 中运行

先右键运行：

```text
examples/10_approval_audit_chain.py
```

重点观察：

1. 第一条 `chain_position=1`，前驱为 64 个零；
2. 第二条 `previous_event_hash` 等于第一条 `event_hash`；
3. `chain_valid=True`；
4. 输出不存在 reason、idempotency_key、comment 或完整 JWT。

要验证真实 API，启动 Uvicorn，再运行 `examples/09_generate_dev_tokens.py`。脚本会产生 customer、
reviewer、auditor 三枚职责隔离的短期 Token。在 Swagger 中依次：

1. 使用 customer Token 调用 `/api/v1/chat` 发起退货并保存 `thread_id`；
2. 切换 reviewer Token 调用 `/api/v1/approvals/{thread_id}`；
3. 切换 auditor Token 调用 `GET /api/v1/audit/approvals/{thread_id}`。

项目默认 SQLite 模式把审计表放在：

```text
D:\serviceops-agent\data\runtime\serviceops.sqlite3
```

它与 `return_requests` 同属业务数据库，但与 LangGraph 的 `checkpoints.sqlite3` 分开。SQLite 是
Python 自带的嵌入式数据库，不需要另外下载服务端程序。

## 本步测试

新增回归覆盖：

- 两节点哈希链位置、前驱和完整性；
- 同决定重试与 Token 续期幂等；
- 不同审批主体的事件冲突；
- SQLite 新实例跨重启读取；
- 普通 UPDATE/DELETE 被只追加触发器拒绝；
- 管理员删除触发器并篡改主体后，哈希重算失败；
- auditor Token 只有 `audit:read`；
- customer/reviewer 不能读审计接口；
- API 响应不出现原因、备注、幂等键或 Bearer Token；
- SQLite AgentRuntime 重建后仍保留完整审计链。

运行：

```powershell
uv run ruff check .
uv run mypy src
uv run pytest -q
```

## 面试追问与回答方向

- 为什么不能只依赖 LangGraph Checkpoint？
  Checkpoint 面向工作流恢复，会包含较多运行状态，也可能有清理策略；安全审计需要独立、最小化、
  长期保留和职责隔离的数据契约。
- 为什么先记录决定再执行？
  这样崩溃后仍能识别已经授权但未获得终态的流程；相反顺序会出现业务写入成功却完全没有授权
  证据的窗口。
- 为什么不把备注原文存进审计表？
  自由文本可能包含隐私、Token、换行注入或无关敏感信息。当前需求只需证明备注与审批时一致，
  因此保存规范化摘要；有合规保留要求时应使用独立加密字段和更严格访问策略。
- SHA-256 哈希链防得住 DBA 吗？
  能检测只改字段或删除中间事件的常见篡改，挡不住 DBA 重写整库并重算哈希。生产需要外部锚点、
  数字签名、WORM 存储和权限隔离。
- 审计写入和业务写入是一个事务吗？
  当前不是。决定事件、LangGraph Checkpoint、业务写入和结果事件跨多个事务；项目通过顺序和幂等
  缩小风险，但生产应增加 transactional outbox、对账补偿和告警。
- 为什么按 thread 建链而不是全局一条链？
  线程链便于局部查询和故障隔离；全局链更容易证明总顺序，但会制造单点写入竞争。生产可用线程
  链加定期全局 Merkle Root/批次签名折中。
