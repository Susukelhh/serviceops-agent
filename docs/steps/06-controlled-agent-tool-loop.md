# 第六步：有界、可停止、可审计的 Agent 工具循环

## 本步目标

前五步已经覆盖分类、RAG、受约束生成和一个安全订单工具，但原订单节点仍按固定代码顺序完成
“提取订单号 → 调工具 → 回答”。第六步把它改造成真正可观察的 Agent 循环：

```text
initialize_order_agent
→ plan_order_action
→ execute_order_tool
→ observation 写回 State
→ plan_order_action
→ call_tool / finish / clarify / handoff
```

一次问题包含 `SO100001` 和 `SO100002` 时，工具节点会通过 LangGraph 回边实际执行两次，而不是
在一个普通函数内部循环后伪装成 Agent。

## 为什么没有直接套用预制 ReAct Agent

本项目需要在面试中解释每一条企业边界，因此显式定义节点和条件边：

- 哪个节点允许模型决策；
- 哪个节点拥有实际工具执行权；
- 什么时候形成回边；
- 哪些失败必须结束并转人工；
- 最大次数和重复调用在哪里被确定性拦截。

预制 Agent 适合快速原型，但隐藏控制流后，很难证明身份、预算、异常和停止条件真的由服务端
保证。这里仍使用 LangChain Model/Tool 接口，只是不把关键安全行为交给黑盒执行器。

## 两种规划器

### 确定性规划器

默认 `deterministic` 从问题中提取所有唯一 `SO` 订单号，再与 `ToolExecutionRecord` 历史比较：

1. 没有订单号：`clarify`；
2. 存在未查询订单：每轮只返回一个 `call_tool`；
3. 所有订单已有观察：`finish`。

它完全离线、零费用、结果稳定，是 CI 和故障隔离基线。

### 真实 LLM 规划器

`llm` 使用 LangChain `with_structured_output` 让千问返回 `ToolCallPlan`，动作只能是：

- `call_tool`；
- `finish`；
- `clarify`；
- `handoff`。

计划 Schema 使用扁平 `order_id`，不允许模型提交任意参数字典。服务端再通过
`plan.tool_arguments()` 构造 `{"order_id": ...}`，可信 `user_id` 始终由工具闭包注入。

开发过程中发现，千问兼容接口对嵌套通用 `arguments` Schema 曾返回非法函数参数；改为针对当前
工具的扁平强类型字段后真实链路稳定通过。这也是为什么生产项目需要真实服务商集成测试，不能
只用 Mock 证明结构化输出“理论可用”。

配置方式：

```dotenv
SERVICEOPS_AGENT_PLANNER_BACKEND=llm
SERVICEOPS_AGENT_MAX_TOOL_STEPS=3
```

2026-08-20 已使用本机 `qwen-plus` 验证：真实模型分类订单意图，规划一次
`get_order_status(SO100001)`，观察安全工具结果后规划 `finish`，最终工具次数为 1、停止原因为
`completed`。API Key 没有进入日志、State、文档或输出。

## 七项执行边界

### 1. 工具名称白名单

`ToolCallPlan` 只约束输出结构，不代表模型取得执行权。执行器目前只接受
`get_order_status`。测试规划器即使返回结构合法的 `delete_order`，实际调用次数仍为零。

### 2. 参数二次校验

计划先经过 `ToolCallPlan`，服务端构造参数后还要经过 LangChain Tool 的 `OrderLookupInput`。
工具返回值再经过 `OrderLookupResult`；最终汇总前还会重新校验历史观察。

### 3. 可信身份绑定

模型 Schema 中没有 `user_id`。执行器从 API State 读取可信身份，再创建工具闭包。用户问题写
“改成 user-002”不会改变仓库实际查询身份。

### 4. 最大工具步数

`agent_max_tool_steps` 默认是 3，范围限制为 1 到 10。规划节点和执行节点都在第 N+1 次调用前
检查预算，形成纵深防御。达到上限后不返回部分结果冒充完整完成，而是转人工。

### 5. 请求内重复调用检测

执行器对“工具名 + 规范参数 JSON”计算 SHA-256 指纹。参数字典顺序不影响摘要；工具名不同会
进入不同指纹空间。相同指纹第二次出现时不会执行工具，并以 `duplicate_tool_call` 停止。

这只是请求内去重，不应夸大成生产级跨进程幂等。写工具还需要数据库唯一键、幂等表或业务系统
支持的 idempotency key。

### 6. 工具异常边界

仓库异常、Tool Schema 错误和结果结构错误不会击穿 FastAPI。失败记录只保存有限错误码，原始
异常正文和半截结果不会成为下一轮模型观察，用户收到确定性人工接管说明。

### 7. 明确停止条件

正常停止包括 `completed` 和 `needs_clarification`；安全停止包括规划故障、未知工具、身份缺失、
重复调用、最大步数和工具异常。未知状态默认人工，不能通过新增任意字符串动态跳转图节点。

## State 中新增的重点字段

- `planned_tool_call`：尚未被执行器信任的一步计划；
- `tool_execution_records`：成功或失败的强类型观察历史；
- `tool_call_count`：真实执行尝试次数；
- `tool_call_fingerprints`：请求内已尝试调用摘要；
- `queried_order_ids`：多订单实际执行顺序；
- `agent_next_action`：条件边读取的有限动作；
- `agent_stop_reason`：最终停止原因；
- `agent_failure_code`：脱敏内部失败类别。

API 新增返回 `queried_order_ids`、`tool_call_count` 和 `agent_stop_reason`，方便当前学习、测试和
面试演示。生产接口通常不会公开完整内部事件，而是写入 Trace 平台。

## 在 PyCharm 中运行

右键运行：

```text
examples/06_controlled_tool_loop.py
```

第一个场景重点观察：

```text
planned_tool_call
→ order_tool_executed
→ planned_tool_call
→ order_tool_executed
→ planned_finish
→ order_agent_completed
```

第二个场景把最大步数设为 1。第一条观察后，第二个工具计划会被
`order_agent_max_tool_steps_blocked` 拦截并转人工。

## 本步测试

- 确定性规划器按唯一订单顺序逐一调用并停止；
- 没有订单号时零工具调用并请求澄清；
- 动作与工具字段矛盾时 Pydantic 拒绝计划；
- 指纹与参数字典顺序无关，但区分工具名称；
- 两个订单真实经过两次 LangGraph 工具回边；
- 卡住的规划器重复调用只实际执行一次；
- 第 N+1 次调用在执行前被最大步数门拦截；
- 未授权 `delete_order` 从未执行；
- 仓库异常转换为失败观察和脱敏人工文案；
- 原有订单越权、RAG、引用白名单、API 和模型故障测试不回归。

## 面试追问与回答方向

- 结构化 ToolCall 为什么仍不能直接执行？
  Schema 只保证形状，工具权限、身份、预算、幂等和业务参数仍由执行器验证。
- 如何证明这不是普通 for 循环？
  每次工具观察写回 State，条件边回到规划节点；事件轨迹中能看到节点实际重复执行。
- `recursion_limit` 能否替代业务最大步数？
  不能。它是框架最后保险，业务预算应在调用前检查并产生可解释停止原因。
- 只读工具为什么也做重复检测？
  防止死循环、费用放大和下游压力，并为未来写工具建立控制模式。
- 当前设计算生产级幂等吗？
  不算。它只覆盖单请求，生产写操作还需持久化幂等键和业务唯一约束。
- 为什么工具结果不直接交给模型生成答案？
  订单状态和物流号属于高确定性事实，代码汇总更稳定；模型负责规划，不负责改写关键事实。
