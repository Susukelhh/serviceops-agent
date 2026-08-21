# 第 21 步：ServiceOps Agent 五分钟面试演示 Runbook

这份 Runbook 的作用类似飞机起飞前检查单：它不是帮你临场背诵全部代码，而是保证环境、演示顺序和
技术表达在紧张情况下仍然稳定。默认演示使用 Docker 中的 `mock + deterministic + hash embedding`，
不会消耗千问额度；所有“模型效果”数字必须单独引用第 14 步真实千问实验报告。

## 一、PyCharm 一键运行配置

在“运行/调试配置”中新建 Python 配置：

- 名称：`21-一键面试演示`；
- Python 解释器：`D:\serviceops-agent\.venv\Scripts\python.exe`；
- 脚本路径：`D:\serviceops-agent\examples\21_interview_demo.py`；
- 参数：留空；
- 工作目录：`D:\serviceops-agent`；
- 环境变量：`PYTHONUTF8=1`；
- 勾选“将内容根添加到 PYTHONPATH”和“将源根添加到 PYTHONPATH”。

直接运行后，程序依次完成：

1. 检查 Docker Compose 配置；
2. 重新构建并等待 PostgreSQL、agent-a、agent-b、Nginx 健康；
3. 验证真实订单工具和 Checkpoint 回放；
4. 在 PyCharm 控制台打印本次短期 Token；
5. 打开 `http://127.0.0.1:8000/console/`。

代码没有变化、只是面试前快速复检时，可以把参数改为：

```text
--no-build
```

如果只检查环境、不希望自动打开浏览器或打印 Token，可以使用：

```text
--no-build --no-browser --no-tokens
```

## 二、面试前 15 分钟检查

- Docker Desktop 左下角显示 `Engine running`；
- 运行 `21-一键面试演示`，最终出现 `面试演示环境 READY`；
- 确认报告中的 `status` 为 `pass`：
  `D:\serviceops-agent\data\runtime\interview_demo_preflight_report.json`；
- 把 customer、reviewer、auditor、developer Token 粘贴到网页对应输入框；
- 浏览器缩放保持 100%，提前按一次 `Ctrl+F5`；
- 关闭聊天软件弹窗、系统更新提示和会遮挡页面的窗口；
- 不要在共享屏幕时展示 `.env`、完整 Token 或千问 API Key。

## 三、五分钟固定演示顺序

### 0:00—0:40：问题与架构边界

建议说：

> 这是一个企业售后 Agent。模型只负责有限决策，LangGraph 管理流程，确定性工具执行订单查询，
> PostgreSQL 同时保存工作流 Checkpoint 和业务记录，写操作必须经过人工审批。

页面动作：先指向左侧四个演示场景和顶部 readiness，不要立即展开所有基础设施细节。

### 0:40—1:40：订单工具与状态回放

页面动作：点击“订单工具调用”，运行 `查询订单 SO100001 到哪了`，再进入下方 Checkpoint 专注大屏。

建议说：

> 这里不是模型直接编答案。状态图先识别意图，再生成强类型工具计划，服务端做工具白名单和用户身份
> 绑定，执行 `get_order_status` 后把观察结果写回 State，再由图决定是否结束。

需要指出的证据：`intent=order_status`、`planned_tool_call`、`execute_order_tool`、`tool_result` 和 9 个
按时间排列的 Checkpoint。

### 1:40—2:30：有据 RAG

页面动作：运行 `发票税号写错了怎么办`，指出回答下方的文档标题、版本、生效日期和检索分数。

建议说：

> RAG 不是“搜到就回答”。知识源先做发布状态治理，检索结果通过分数和权限门槛后才能进入回答层；
> 没有公共证据或引用不合法时会转人工，而不是让模型自由发挥。

### 2:30—3:15：越权防护

页面动作：运行 `查询订单 SO200001 到哪了`。

建议说：

> 用户身份只信 JWT 的 `sub`，订单工具会把这个身份从服务端注入查询条件。即使用户在文本里写别人的
> 订单号，也不能覆盖可信身份，因此不会泄漏 user-002 的订单数据。

### 3:15—4:30：人工审批与恢复

页面动作：运行退货场景，指出橙色 interrupt；先展示“审批前零写入”，再用 reviewer Token 批准，观察
新 Checkpoint 接到原线程后面，最后用 auditor Token 验证哈希链。

建议说：

> 写操作先生成草案并由 LangGraph interrupt 暂停。审批身份与用户身份分离，批准后通过
> `Command.resume` 恢复同一个 thread。业务记录与 Outbox 同事务提交，审计事件最终形成追加式哈希链。

### 4:30—5:00：工程证据与诚实边界

建议说：

> 项目有 13 条 Agent 黄金样本、真实千问候选实验、JWT/RBAC、OpenTelemetry、双实例共享 PostgreSQL、
> 过载保护和备份恢复演练。本机 Docker 用来证明关键机制，不等同于完整云生产环境。

## 四、面试官常见追问

### 为什么使用 LangGraph，而不是普通顺序代码？

因为订单流程存在“规划—工具—观察—再规划”的环，退货流程还需要 interrupt、外部审批和跨进程恢复。
LangGraph 的价值是显式状态、条件边、可恢复执行与测试边界，而不是单纯把函数画成图。

### Checkpoint 和业务数据库有什么区别？

Checkpoint 保存“Agent 执行到了哪里”；业务表保存“现实业务已经发生了什么”。审批暂停可以只产生
Checkpoint 而不创建退货记录，批准后业务写入才发生。

### 为什么不展示模型完整思维链？

隐藏推理原文不是可靠审计证据，还可能泄漏提示词和内部信息。项目展示的是可验证的结构化结论、
路由原因、工具计划、输入输出摘要、引用和状态变化。

### 为什么有四种网页 Token？

customer 对话、reviewer 审批、auditor 读审计链、developer 读脱敏调试轨迹。职责分离可以证明任一角色
拿到 Token 后都不能自动拥有全部高权限。

### 这个本地项目距离生产还差什么？

仍需要企业密钥管理、TLS/网关身份、正式云数据库高可用、对象存储备份、告警值班、真实容量规划、
灰度发布、隐私合规评审和长期在线评测。回答时不要把本机双实例称为完整生产集群。

## 五、演示结束后的安全收尾

- 关闭共享屏幕后再处理 Token；短期 Token 到期后自动失效；
- 关闭浏览器页面会清空页面内存中的 Token；
- 需要停止容器但保留 PostgreSQL 数据时运行 `docker compose down`；
- 不要运行 `docker compose down --volumes`，除非明确想删除本地演示数据。

常见故障请查看：[本地面试演示排障表](local-demo-troubleshooting.md)。
