# ServiceOps Agent 本地演示排障表

先看错误发生在哪个编号步骤。第 21 步启动器遇到失败会立即停止，不会继续输出 Token 或打开一个不可用页面。

| 现象 | 白话原因 | 优先检查 | 处理方法 |
|---|---|---|---|
| 找不到 `docker.exe` | 启动器找不到 Docker 工具 | Docker Desktop 是否安装 | 启动 Docker Desktop；确认左下角 `Engine running` 后重试 |
| Compose 配置失败 | 项目“装箱清单”语法或变量有问题 | 上方第一条错误 | 不要继续演示；先恢复 `compose.yaml` 的合法配置 |
| 等待环境超时 | 某个容器没有健康启动 | Docker Desktop 的 Containers 页面 | 展开 `serviceops-agent`，查看哪个角色不是 healthy |
| 端口 8000 被占用 | 另一个程序占用了演示入口 | PowerShell 执行 `Get-NetTCPConnection -LocalPort 8000` | 关闭占用程序，或先停止旧的同名服务 |
| 页面仍是旧布局 | 浏览器缓存或镜像未重建 | 是否使用默认构建模式 | 重新运行第 21 步并按 `Ctrl+F5` |
| Chat 返回 401 | customer Token 缺失、过期或复制不完整 | 身份面板普通用户输入框 | 重新运行第 09 步或第 21 步并完整复制 Token |
| 审批返回 403 | 把 customer Token 当成 reviewer 使用 | 审批人输入框 | 粘贴 `return:approve` 的 reviewer Token |
| 调试读取返回 403 | developer Token 不正确 | 开发调试输入框 | 粘贴 `debug:read` 的 developer Token |
| 调试读取返回 404 | 生产环境关闭调试接口，或线程不存在 | 当前是 development Compose 吗 | 本地演示使用项目默认 Compose；先运行一个新场景 |
| 突发操作出现 429 | Nginx 正在保护后端 | 是否连续快速点击 | 等待约 1—3 秒再试，不要把保护性拒绝当成系统崩溃 |
| 冒烟没有找到工具节点 | 旧代码、旧镜像或图回归 | 第 20.2 步报告 | 使用默认构建模式重试；仍失败则先运行完整测试 |
| 中文输出乱码 | 旧 PowerShell 编码显示问题 | 源文件是否仍是 UTF-8 | PyCharm 配置 `PYTHONUTF8=1`；乱码通常不代表代码损坏 |

## 不要用这些“快速修复”

- 不要为了演示临时关闭 JWT；这会破坏项目最重要的权限证据。
- 不要把 Token 写死进 HTML、JavaScript、`.env.example` 或截图。
- 不要把 Qwen 配成 Docker 默认后端；本地演示应保持无费用、可重复。
- 不要看到数据库问题就执行 `docker compose down --volumes`；它会删除具名卷里的本地数据。
- 不要在面试现场临时升级 Docker、Python、LangGraph 或 PostgreSQL 版本。

## 最后兜底

如果现场网络或电脑环境确实无法恢复，直接展示已经保存的离线评测、千问候选报告和架构文档，说明：
“本地运行环境当前异常，但下面这些报告来自版本控制用例和之前的真实演练，不是临时编造的截图。”
诚实说明故障边界，通常比删除安全校验强行演示更专业。
