# PyCharm 导入与运行指南

## 一、打开正确的目录

在 PyCharm 欢迎页点击 **Open**，选择：

```text
D:\serviceops-agent
```

必须打开项目根目录，不能只打开 `src`。根目录中包含 `pyproject.toml`，PyCharm 会通过它
识别依赖、测试和 Python 版本要求。

## 二、选择项目解释器

依次进入：

```text
File → Settings → Project: serviceops-agent → Python Interpreter
```

点击 **Add Interpreter → Add Local Interpreter → Existing**，选择：

```text
D:\serviceops-agent\.venv\Scripts\python.exe
```

应用设置后，在 PyCharm Terminal 中执行：

```powershell
python --version
```

预期显示 `Python 3.12.x`。如果显示 3.10，说明选中了系统解释器，需要重新执行上面的步骤。

## 三、检查 src 布局

本项目采用企业 Python 项目常见的 `src` 布局，包实际位于：

```text
src\serviceops_agent
```

正常情况下，`uv sync --dev` 已经以可编辑方式安装了项目。若编辑器仍提示
`Cannot find reference serviceops_agent`，右键 `src` 目录并选择：

```text
Mark Directory as → Sources Root
```

## 四、创建 API 运行配置

点击右上角 **Add Configuration → Add New Configuration → Python**，填写：

| 配置项 | 内容 |
|---|---|
| Name | `ServiceOps API` |
| Run | `Module name` |
| Module name | `uvicorn` |
| Parameters | `serviceops_agent.api.app:app --reload` |
| Working directory | `D:\serviceops-agent` |
| Python interpreter | 项目的 Python 3.12 `.venv` |

点击运行后，控制台出现 `Uvicorn running on http://127.0.0.1:8000` 即表示成功。
浏览器访问：

- API 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

## 五、运行测试

进入：

```text
File → Settings → Tools → Python Integrated Tools
```

将 **Default test runner** 设为 `pytest`。然后右键 `tests` 目录，选择
**Run 'pytest in tests'**。项目会随着步骤持续增加测试数量，因此不要背固定总数；应以本次终端显示的
全部通过、零失败为准。

也可以直接在 PyCharm Terminal 中运行：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy src
```

## 六、中文显示异常

源代码统一保存为 UTF-8。若 PyCharm 中文显示异常，检查：

```text
File → Settings → Editor → File Encodings
```

将 Global Encoding 和 Project Encoding 都设置为 `UTF-8`。Windows PowerShell 旧控制台
偶尔会出现输出乱码，但不代表源文件损坏；PyCharm 控制台通常可以直接正确显示。

## 七、配置模型密钥

项目默认离线运行，不需要模型密钥。需要千问分类、Embedding 或受约束生成时，把
`.env.example` 复制为本地 `.env`；真实密钥只保存在 `.env` 中。该文件已经被 `.gitignore`
排除，不能提交到仓库。

## 八、运行第五步学习示例

在项目树中找到并右键运行：

```text
examples/05_rag_evaluation.py
```

该示例会在导入项目模块前强制使用 Hash Embedding 和内存 Qdrant，因此即使 `.env` 已经配置
千问，也不会产生 API 调用。正常输出应显示 11 条样本，`Recall@3` 和 `MRR@3` 均为 1.000，
负例误召回率为 0.000。

## 九、运行第六步工具循环示例

在项目树中右键运行：

```text
examples/06_controlled_tool_loop.py
```

第一个场景会查询 `SO100001` 和 `SO100002`，控制台应出现两次
`graph:order_agent_planned_tool_call` 和两次 `graph:order_tool_executed`，最后停止原因为
`completed`。第二个场景把最大工具步数设为 1，第二次调用会在执行前停止，最终进入人工路径。

该示例强制使用 `deterministic` 规划器，不会读取 `.env` 中的真实千问开关，也不会产生费用。

## 十、运行第七步人工审批示例

在项目树中右键运行：

```text
examples/07_human_approval_return.py
```

控制台会展示四个连续场景：审批前 `interrupt` 且记录数为 0；批准后生成 `RR-...`；拒绝后
不新增；新线程复用同一幂等键后返回原编号。该示例使用 `InMemorySaver`，只适合单进程学习，
关闭 Python 进程后审批快照会消失。

## 十一、运行第八步 SQLite 重启恢复示例

在项目树中右键运行：

```text
examples/08_sqlite_restart_recovery.py
```

示例会创建临时 Checkpoint/业务数据库，依次关闭并重建三套运行时。重点观察第二套运行时仍能
找到第一套留下的 `request_return_approval`，第三套仍能读取完成状态和相同 `RR-...` 编号。
示例结束后临时文件会自动清理，不会改动你的 `data/runtime` 正式开发数据。

正常启动 Uvicorn 时，项目默认使用 `data/runtime` 下的 SQLite 文件。要手工验证真实进程重启，
先发起退货并保存 `thread_id`，停止并重新启动 API，再使用原 ID 调审批接口。

## 十二、生成第九步本地 JWT

在项目树中右键运行：

```text
examples/09_generate_dev_tokens.py
```

控制台会输出五枚三十分钟短期 Token：

- `user-001`：只有 `agent:chat`，用于 `/api/v1/chat`；
- `reviewer-001`：只有 `return:approve`，用于 `/api/v1/approvals/{thread_id}`；
- `auditor-001`：只有 `audit:read`，用于 `/api/v1/audit/approvals/{thread_id}`；
- `operator-001`：只有 `operations:reconcile`，用于 `/api/v1/internal/outbox/reconcile`。
- `developer-001`：只有 `debug:read`，用于 development/test 的脱敏 Checkpoint 回放。

打开 `http://127.0.0.1:8000/docs`，点击右上角 **Authorize**，只粘贴 Token 本身，不要手动加
`Bearer`。切换普通用户、审批人、审计员、运维员和开发调试接口时需要重新 Authorize。Token 虽然只用于本地开发，也不要
发到聊天、截图分享或提交 Git。

## 十三、运行第十步审批审计示例

在项目树中右键运行：

```text
examples/10_approval_audit_chain.py
```

该示例完全离线。重点对比第一条 `event_hash` 和第二条 `previous_event_hash`，两者应该相同，
最后显示完整链校验为 `True`。输出中只有草案/备注摘要，不会出现原因、幂等键、备注或完整 JWT。

真实 API 场景需要依次切换 customer、reviewer、auditor Token。默认 SQLite 数据位于
`D:\serviceops-agent\data\runtime\serviceops.sqlite3`，无需单独安装 SQLite；PyCharm Pro 可在
Database 工具窗口直接添加该文件并查看 `approval_audit_events` 表。

## 十四、运行第十一步 OpenTelemetry 示例

在项目树中右键运行：

```text
examples/11_observability_trace.py
```

示例强制使用完全离线后端，不会调用千问。Console Exporter 会输出较长 JSON，属于正常现象。
先复制开头的 32 位 Trace ID，在 PyCharm Console 搜索它，确认根业务 Span、所有 LangGraph 节点
Span 和两条 JSON 日志使用同一 Trace。`execute_order_tool` 应出现两次，Metrics 中工具计数为 2。

真实 API 默认同样使用 Console Exporter。启动后除 `/docs` 外可以访问：

- `http://127.0.0.1:8000/health`：只检查进程存活；
- `http://127.0.0.1:8000/ready`：真实读取 Checkpointer、退货业务库、事务 Outbox 和审计库。

如果 Console 输出影响学习，可在本地 `.env` 设置 `SERVICEOPS_TELEMETRY_EXPORTER=none`；需要发送到
Collector 时改成 `otlp_http`，并确保部署环境已经提供 `http://127.0.0.1:4318` 或自定义端点。

## 十五、运行第十二步 Transactional Outbox 恢复示例

在项目树中右键运行：

```text
examples/12_transactional_outbox_recovery.py
```

该示例会创建临时 SQLite 数据库，模拟“业务事务已经提交、审计投递暂时失败”，随后重建运行时并
执行补偿。重点观察：退货记录与 `pending` Outbox 事件在同一事务出现；补偿成功后事件变成
`delivered`；再次补偿不会重复追加审计事件。这对应企业系统里解决跨库双写窗口的常用模式。

## 十六、运行第十三步端到端 Agent 离线评测

在项目树中右键运行：

```text
examples/13_agent_end_to_end_evaluation.py
```

无需千问 API Key，也不会读取你的真实模型配置。脚本会固定使用 mock 意图分类、确定性工具规划、
Hash Embedding、内存 Qdrant、摘录式 FAQ 回答和内存 Checkpointer，完整执行 13 条 LangGraph 用例。

正常结果应同时看到：

- `Cases: 13/13 passed`；
- Routing、Tool trajectory、Response contract、Safety invariant 均为 `100.00%`；
- `Quality gate: PASS`。

逐样本证据会写入：

```text
D:\serviceops-agent\data\runtime\agent_end_to_end_report.json
```

如果你故意改错黄金集中的期望意图，脚本会输出稳定失败码并以进程码 `1` 结束，因此以后可以直接接入
GitHub Actions。学习时不要只记“13 条通过”，要能解释四层评测为什么分别检查最终答案、路由、真实
工具轨迹和外部副作用。

## 十七、运行第十四步真实千问候选实验

该示例会调用真实模型并可能产生费用，因此直接右键运行只会安全退出，不会发出请求。先确认项目根目录
`.env` 已配置以下三项，API Key 不要截图、提交 Git 或发到聊天：

```text
SERVICEOPS_LLM_MODEL=qwen-plus
SERVICEOPS_LLM_API_KEY=你的百炼密钥
SERVICEOPS_LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

在 PyCharm 顶部选择 **Run → Edit Configurations**，新建 **Python** 配置：

- Name：`14 Qwen candidate - 1 trial`；
- Script path：`D:\serviceops-agent\examples\14_qwen_candidate_experiment.py`；
- Parameters：`--confirm-paid-api --trials 1`；
- Working directory：`D:\serviceops-agent`；
- Python interpreter：项目现有 `.venv`。

第一次先跑一轮。当前 13 条参考路径会在发请求前提示约 `24 次/轮`，真实数量可能因 SDK 重试或模型
错误路由变化。确认额度和报告正常后，把 Parameters 改为只有 `--confirm-paid-api`，即可按配置文件
默认重复三轮。

首次 `qwen-plus` 单轮实验 `1.0.0` 得到原始 9/13，其中三条是 LLM 与确定性分类器事件命名不同造成的
评测假失败，一条是内部补偿政策被误分到 FAQ，但知识治理层仍安全转人工。修复后实验已经升级到
`1.1.0`，所以不要把两份报告当作同一处理组直接求平均。

报告写入：

```text
D:\serviceops-agent\data\runtime\qwen_candidate_experiment_report.json
```

重点不要只看最好一轮，而要检查：平均整体通过率、最差轮整体通过率、全轮稳定样本率、平均安全
不变量准确率，以及每个不稳定 case 的 `observed_violations`。晋级失败返回进程码 `1`，未提供付费
确认返回进程码 `2`。

修复后的单轮报告还应检查：

- `experiment_version` 必须是 `1.1.0`；
- 每条结果都包含有限 `actual_events`；
- 发票、天气、可审批退货三条不再出现 `required_event_sequence_missing`；
- 响应契约与安全不变量继续保持 100%；
- 只有单轮结果正常后才运行默认三轮，不能把一次通过称为稳定性通过。

上传 GitHub 后，普通 `ServiceOps offline quality gate` 不需要任何密钥。若要运行手工千问工作流，
在仓库 **Settings → Secrets and variables → Actions** 新增 Secret `SERVICEOPS_LLM_API_KEY`；模型名和
Base URL 可以分别放入同名 Repository Variables。然后到 Actions 中手工运行
`ServiceOps Qwen candidate experiment`，不要让真实模型工作流响应 Pull Request 事件。

## 十八、运行第十五步容器部署冒烟

当前电脑已经安装 WSL 2 与 Docker Desktop，并在 2026-08-20 完成真实镜像构建和容器冒烟。重启
PyCharm 后，在 Terminal 中先确认下面两条命令都能显示客户端和服务端版本：

```powershell
docker version
docker compose version
```

然后在项目根目录依次执行：

```powershell
docker compose config
docker compose build
docker compose up --detach --wait --wait-timeout 120
docker compose ps
uv run python examples/15_container_smoke_test.py
```

正常结果为：

```text
PASS: liveness=ok, readiness=ready(4/4), unauthenticated_chat=401
```

这分别证明 Web 进程存活、四个 PostgreSQL 持久化边界可以接流量，以及未带 JWT 的业务请求仍被拒绝。
`migrate` 显示 `Exited (0)` 是一次性迁移成功后的正常状态；`gateway`、`agent-a`、`agent-b` 和
`postgres` 应显示 `healthy`。若本机代理环境导致构建阶段下载
Python 依赖超时，可使用第十五步文档中的 `HTTP_PROXY/HTTPS_PROXY` build-arg 命令；不要把代理地址
写进 Dockerfile。
如果失败，先运行：

```powershell
docker compose logs --tail 100 gateway agent-a agent-b migrate postgres
```

PyCharm Professional 可以在
**Settings → Build, Execution, Deployment → Docker** 连接 Docker Desktop，再右键 `compose.yaml`
运行并从 Services 窗口查看容器。社区版直接使用 Terminal 即可；日常 Python 解释器仍然选择
`D:\serviceops-agent\.venv\Scripts\python.exe`，不需要改成容器解释器。

运行第十七步跨实例自动演练：

```powershell
uv run python examples/17_multi_instance_failover.py
```

在 PyCharm 新建 Python 运行配置时填写：

- Script path：`D:\serviceops-agent\examples\17_multi_instance_failover.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

预期看到 `PASS 第17步`，并且脚本退出后两只 Agent 都恢复为 `healthy`。

运行第十八步容量与恢复基线：

```powershell
uv run python examples/18_load_and_resilience_baseline.py
```

PyCharm Python 运行配置填写：

- Script path：`D:\serviceops-agent\examples\18_load_and_resilience_baseline.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

默认会发送 12 条正常请求和 40 条瞬时请求。预期正常阶段全部 200，突发阶段同时出现 200 与 429，
没有异常 5xx，三秒后恢复 200/readiness 4/4。报告位于
`D:\serviceops-agent\data\runtime\load_resilience_step18_report.json`。

运行第十九步 PostgreSQL 备份与隔离恢复演练：

```powershell
uv run python examples/19_postgres_backup_restore_drill.py
```

在 PyCharm 新建 Python 运行配置时填写：

- Name：`19-PostgreSQL备份恢复演练`；
- Run kind：`Script`；
- Script path：`D:\serviceops-agent\examples\19_postgres_backup_restore_drill.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Environment variables：只填写 `PYTHONUTF8=1`，不要复制数据库密码；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`；
- `Add content roots to PYTHONPATH` 与 `Add source roots to PYTHONPATH` 保持勾选。

运行前先在 Docker Desktop 左下角确认 `Engine running`，并在 PyCharm Terminal 执行
`docker compose ps`。预期看到 `PASS 第19步`、8 张验证表和三个绝对文件路径。随后可用下面命令确认
没有遗留的临时恢复数据库：

```powershell
docker compose exec -T postgres psql -U serviceops -d serviceops -tAc `
  "SELECT datname FROM pg_database WHERE datname LIKE 'serviceops_restore_drill_%';"
```

正常结果为空。备份位于 `D:\serviceops-agent\data\backups`，可能包含业务、审计和对话状态，不要提交
Git、上传公开网盘或发给无权限人员。

## 二十一、打开第二十步 Agent 可视化控制台

控制台由 FastAPI 与 Docker 直接提供，不需要在 PyCharm 单独运行前端开发服务器。先在 Terminal 执行：

```powershell
docker compose up --detach --build --wait --wait-timeout 120
docker compose ps
```

然后创建或复用 `09_generate_dev_tokens.py` 的 Python 运行配置：

- Name：`09-生成本地演示Token`；
- Script path：`D:\serviceops-agent\examples\09_generate_dev_tokens.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Environment variables：`PYTHONUTF8=1`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

运行后会输出五种角色 Token。控制台目前需要其中四种：

1. 普通用户 Token：调用 Agent 对话；
2. 退货审批 Token：批准或拒绝暂停线程；
3. 审计员 Token：验证审批哈希链。
4. 开发调试 Token：读取脱敏的 StateSnapshot/Checkpoint 历史。

在浏览器打开：

```text
http://127.0.0.1:8000/
```

根路径会进入 `/console/`。点击右上角“身份与权限”，把四枚 Token 分别粘贴到对应密码框，点击“仅在
本页使用”。Token 只存在当前网页内存，刷新页面后自动消失，不要把 Token 保存到源码、截图或聊天。

建议按下面顺序演示：

1. `查询订单 SO100001 到哪了`：右侧出现规划与工具时间线；
2. `发票税号写错了怎么办`：回答下方出现知识标题、版本与生效日期；
3. `查询订单 SO200001 到哪了`：证明 Token 身份不能读取 user-002 订单；
4. 退货场景：出现黄色审批卡，审批后恢复原线程，再点击“验证链”。

每次请求完成后，页面下方的“Agent 执行回放工作台”会在配置 developer Token 时自动读取真实线程历史。
工作台默认占满内容宽度；点击右上角“专注大屏”后会隐藏侧栏和对话，只保留调试信息。验证顺序：

1. 先看最下方最新 Checkpoint 是否显示“图已到达终点”；
2. 点击最左侧 `C1`，再逐次点击右箭头；
3. 在“状态变化”中观察 `intent`、`planned_tool_call`、`tool_result`、`answer` 何时出现；
4. 在“Checkpoint”中观察 `checkpoint_id`、父快照、`metadata.step` 和 `next`；
5. 退货暂停时找到橙色 `I` 步骤，确认 interrupt 存在且写工具尚未执行；批准后观察新快照接在后面。
6. 点击“专注大屏”，使用左右方向键切换步骤，最后按 `Esc` 返回完整控制台。

如果页面仍显示旧版 Swagger 或 404，说明容器还是第十九步旧镜像。重新执行上面的 `--build` 命令，
不要只刷新浏览器。

还可以新建自动冒烟运行配置：

- Name：`20-Agent控制台冒烟`；
- Script path：`D:\serviceops-agent\examples\20_agent_console_smoke_test.py`；
- Parameters：留空；
- Working directory：`D:\serviceops-agent`；
- Environment variables：`PYTHONUTF8=1`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`。

预期输出 `PASS 第20.2步`，并显示 Checkpoint 数量与 `execute_order_tool`。该脚本只查询本人订单，
不创建退货也不调用千问。

## 二十二、配置第 21 步一键面试演示

新建一个 Python 运行配置：

- Name：`21-一键面试演示`；
- Script path：`D:\serviceops-agent\examples\21_interview_demo.py`；
- Parameters：第一次运行留空；
- Working directory：`D:\serviceops-agent`；
- Environment variables：`PYTHONUTF8=1`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`；
- 保持“将内容根添加到 PYTHONPATH”和“将源根添加到 PYTHONPATH”开启。

第一次运行会重新构建镜像，所以可能需要几十秒。程序会自动完成 Compose 配置校验、等待四个长期容器
健康、运行第 20.2 步真实冒烟、打印五枚短期 Token，并打开本地控制台。看到下面文字才表示可以开始演示：

```text
=== 面试演示环境 READY ===
```

如果代码和 Docker 镜像都没有变化，可以把 Parameters 改成 `--no-build`，减少重复构建时间。只想做环境
检查时可以填写 `--no-build --no-browser --no-tokens`。这些参数不会关闭 JWT 或绕过真实工具，只是跳过
可选的镜像构建、打开浏览器和打印身份动作。

完整五分钟演示顺序见 `docs/runbooks/21-interview-demo-runbook.md`，现场故障处理见
`docs/runbooks/local-demo-troubleshooting.md`。运行报告位于：

```text
D:\serviceops-agent\data\runtime\interview_demo_preflight_report.json
```

## 二十三、查看第 22 步架构图

在 PyCharm 项目树中打开 `D:\serviceops-agent\docs\architecture.md`。编辑器右上角选择 Markdown 的
“编辑器和预览”模式，可以依次查看：

1. 五层系统总览图；
2. 退货审批、interrupt、恢复和 Outbox 的时序图；
3. Nginx、agent-a、agent-b、migrate、PostgreSQL 与具名卷的部署图。

如果当前 PyCharm 版本没有渲染 Mermaid，直接用浏览器打开下面的纯 HTML 速查图，不需要启动 API：

```text
D:\serviceops-agent\reference\serviceops-architecture-whiteboard.html
```

练习时不要照着大图逐字念。打开 `docs\architecture\interview-whiteboard.md`，按“五个盒子 → 三条分支 →
两条治理虚线”的顺序在纸上重画，目标是 90 秒内完成。

停止服务并保留 PostgreSQL 具名卷：

```powershell
docker compose down
```

`docker compose down --volumes` 会删除本地 Checkpoint、退货、Outbox 和审批审计数据，只有确认不再
需要这些演示记录时才能运行。

## 二十四、查看第 23 步求职材料

在 PyCharm 项目树中展开 `D:\serviceops-agent\career`，按下面顺序打开 Markdown 文件：

1. `research\hangzhou-agent-job-requirements-2026-08.md`：了解当前应该投哪些岗位、哪些技术先不学；
2. `resume\serviceops-agent-resume-draft.md`：查看一页式中文简历内容初稿；
3. `resume\project-evidence-map.md`：逐条确认简历描述能否回到真实代码；
4. `resume\personal-info-checklist.md`：填写生成正式简历需要的真实个人资料。

PyCharm 右上角选择 Markdown 的“编辑器和预览”模式即可阅读。当前初稿故意保留 `[姓名]`、`[学校名称]`
等占位符；没有补齐真实资料前不要直接导出或投递。

只验证第 23 步材料时，新建 Python 运行配置：

- Name：`23-求职材料校验`；
- Module name：`pytest`；
- Parameters：`tests/unit/test_career_materials.py -q`；
- Working directory：`D:\serviceops-agent`；
- Environment variables：`PYTHONUTF8=1`；
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`；
- 保持“将内容根添加到 PYTHONPATH”和“将源根添加到 PYTHONPATH”开启。

预期先看到 3 项测试通过。它们只检查占位符、实验数字和简历诚实边界，不会调用千问、不启动 Docker，
也不会修改数据库。
