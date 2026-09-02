# 第45步：把订单、RAG与审批结果接入结构化记忆

## 这一步解决什么问题

第44步能够读取历史并解析“它、这个订单、那运费呢”，但跨轮结构化记忆还没有随着真实执行结果更新；
退货审批恢复后，会话轮次也要等下一次消息重放才可能从 `waiting_approval` 修复为完成。

第45步把三条链路接起来：

1. 订单工具真正验证归属后，更新可信订单槽位；
2. FAQ答案真正通过grounding后，更新引用文档槽位；
3. 退货审批恢复时，直接同步原会话轮次和记忆。

## “查询过”不等于“验证过”

订单Agent的 `queried_order_ids` 表示工具确实尝试查询过，其中也包含 `found=false` 的订单。`found=false` 可能表示订单不存在，
也可能表示它不属于当前用户，因此绝不能进入 `active_order_id` 或 `recent_order_ids`。

第45步重新校验每条 `ToolExecutionRecord` 和 `OrderLookupResult`，只有同时满足以下条件才保存订单号：

- 工具记录结构合法；
- `succeeded=true`；
- 工具名是服务端白名单中的 `get_order_status`；
- 领域结果再次校验成功；
- `found=true`，证明仓库确实找到了当前JWT用户拥有的订单。

退货订单使用另一条可信来源：`ReturnRequestProposal`。草案只会在订单属于当前用户且已经签收的预检查通过后生成，
所以等待审批时就可以作为可信订单引用；不能直接从用户原文提取后写入记忆。

## 检索命中不等于最终证据

RAG的候选切片和 `has_sufficient_evidence` 还不是最终引用。只有图结果同时满足：

```text
faq_answer_grounded = true
```

并且每个 `Citation` 再次通过Schema校验时，文档ID才能进入 `recent_document_ids`。
该列表最多10项，只保存稳定文档ID，不保存正文、向量或模型答案。

这些ID只是来源追踪和下一轮检索提示，下一轮仍必须重新检索并检查文档当前是否为 `published/public`；
不能因为上一轮引用过就跳过 Evidence Sufficiency 检查。

## 记忆为什么必须从轮次重建

简单的“请求完成后追加到列表”会受并发完成顺序影响。例如序号2先完成、序号1后完成，列表顺序可能与真实对话顺序相反；
旧轮次重放还可能反复把同一个ID移动到末尾，造成记忆版本振荡。

因此第45步把轮次表定义为事实源，把 `ConversationMemory` 定义成可丢弃、可重建的派生索引：

1. 图结果先原子写入对应轮次；
2. 按 `sequence_number` 读取最近50轮；
3. 只选择完成、等待审批或已被审批CAS认领且字段完整的轮次；
4. 按轮次序号重新构造最近订单、最近文档、最后意图和活动焦点；
5. 使用 `memory_version` 乐观CAS写入；
6. 冲突后重新读取事实源再计算，最多尝试5次；
7. 相同源记录重放得到完全相同内容时不增加版本。

所以无论 `seq1 -> seq2` 还是 `seq2 -> seq1` 完成，最终记忆都会收敛到同一结果。

## 活动订单焦点规则

- 最新轮是订单或退货主题，并且只有一个已验证订单：设置为活动订单；
- 最新轮包含多个已验证订单：清空活动订单，后续指代必须澄清；
- 最新订单意图没有可信订单：清空，不能沿用旧订单冒充成功；
- 最新轮切换到FAQ或人工主题：清空活动订单，但历史可信订单仍可保留在最近订单列表中；
- 追问解析器只信结构化活动订单或紧邻的订单/退货轮次，不能跨过一轮FAQ去绑定更早订单。

`last_intent` 和 `current_topic` 只是路由提示，不是业务事实、授权依据或Evidence Sufficiency结论。

## 审批恢复如何避免双执行

会话轮次可以通过唯一 `workflow_thread_id` 找回。审批API在恢复LangGraph前执行以下检查：

1. Checkpoint中的原始 `user_id` 必须非空；
2. 它必须与会话所有者一致，不能使用审批人的身份代替申请人；
3. 会话必须仍处于活动期；
4. Checkpoint草案幂等键必须等于会话轮次幂等键；
5. 轮次必须处于 `waiting_approval`。

审计决定落库后，API先做原子状态比较更新：

```text
waiting_approval -> running
```

只有CAS成功的请求才能调用 `Command.resume`。两个完全相同的审批并发提交时，一个返回200，另一个返回409，
退货写工具只执行一次。

图正常结束后，无论批准还是人工拒绝，该轮都从 `running` 进入 `completed` 并保存最终答案；
恢复或写工具抛异常时进入 `failed`。随后根据终态轮次重建结构化记忆。

旧 `/api/v1/chat` 产生的线程没有会话映射，继续使用原审批流程，保持接口兼容。

## 崩溃窗口为什么选择失败关闭

本步复用现有 `running` 状态作为审批认领标记，没有增加租约字段。如果进程在CAS成功后、恢复图之前崩溃，
后续审批会看到该轮已经被接管并返回409，不会自动改回等待态，因为自动解锁可能导致重复执行写操作。

这是明确的失败关闭策略：宁可需要运维核查，也不自动双写。第46步会继续处理生命周期、清理和恢复治理；
生产增强方案可以增加独立 `approving` 状态、认领时间和超时租约。

## Readiness更新

会话仓库已成为多轮API关键依赖，因此 `/ready` 现在真实读取六个边界：

- Checkpointer；
- 会话仓库；
- 退货业务仓库；
- Outbox；
- 审批审计仓库；
- Qdrant知识索引。

任一依赖不可读都会返回503，而 `/health` 仍只表达进程存活。

## 自动测试覆盖

- 内存和SQLite都能按 `workflow_thread_id` 查找轮次；
- 两个并发审批认领只有一个CAS成功；
- 两个并发HTTP审批只有一个200、另一个409，退货记录总数为1；
- 批准后会话轮次立即完成，最终答案和活动订单同步；
- `found=false` 的不存在/越权订单不会进入任何可信订单槽位；
- grounded FAQ实际引用进入文档槽位；
- 较早轮次晚完成不会覆盖较新焦点；
- 相同事实源重放不增加记忆版本；
- 多订单最新轮清空活动订单；
- FAQ主题阻止指代跨越到较早订单；
- 超长文档ID在轮次边界被拒绝，不会等到记忆同步时才产生HTTP 500；
- 审批认领期间记忆不倒退，审批失败后移除失效的等待焦点；
- 原有单轮聊天、审批、审计和Outbox测试继续通过。

## 本步文件

- 可信字段提取与审批同步：`src/serviceops_agent/api/app.py`
- 可重建记忆服务：`src/serviceops_agent/application/conversation_memory.py`
- 追问焦点边界：`src/serviceops_agent/application/conversation_context.py`
- 记忆与状态机契约：`src/serviceops_agent/domain/conversation.py`
- 三后端工作流线程反查：`src/serviceops_agent/infrastructure/conversation_repository.py`
- 记忆测试：`tests/unit/test_conversation_memory.py`
- 仓库并发测试：`tests/unit/test_conversation_repository.py`
- 多轮API和审批测试：`tests/api/test_conversation_api.py`
