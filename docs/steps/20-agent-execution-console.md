# 第二十步：可解释 Agent 运行控制台

> 当前控制台已在第 20.1 步增加第四种 developer 身份和 Checkpoint 单步回放。本文保留第 20 步
> 初版设计记录；新增实现见 [`20-1-checkpoint-teaching-debugger.md`](20-1-checkpoint-teaching-debugger.md)。

## 现实问题

Swagger 能证明接口可调用，JSON 报告能证明评测通过，但面试官很难在几分钟内把这些零散证据拼成完整
Agent 流程。普通聊天框又只显示问题和答案，会把项目最有价值的路由、工具、安全、审批和审计全部藏掉。

因此本步建设的不是商城页面，而是面向演示和内部运维的“Agent 执行工作台”：

```text
左侧                         中间                         右侧
系统 readiness              用户问题                     认证事件
四个固定演示场景             Agent 回答                   意图分类
Swagger/身份配置             RAG 引用                     检索/规划
                             人工审批卡                   工具/响应
                                                          审批哈希链
```

## 页面使用的都是真实后端吗

是。控制台没有维护第二套模拟 Agent：

- 对话调用 `POST /api/v1/chat`；
- 审批调用 `POST /api/v1/approvals/{thread_id}`；
- 审计调用 `GET /api/v1/audit/approvals/{thread_id}`；
- 状态调用 `/health` 与 `/ready`；
- Nginx 仍是 Windows 唯一入口，响应头显示真实 `agent-a/agent-b`；
- PostgreSQL 仍保存 Checkpoint、退货、Outbox 和审计链。

因此页面出现的意图、工具次数、引用、线程和事件都来自当前请求，不是写死的演示动画。

## 为什么不展示“思维链”

后端 `events` 是系统主动定义的业务事件，例如“认证通过、完成意图分类、工具查询成功”。`route_reason`
是短而可审计的路由依据。它们不等于模型逐字隐藏推理过程。

控制台明确展示：

- 哪个节点执行了；
- 走了哪条受控路径；
- 是否调用工具；
- 引用了什么已发布证据；
- 为什么暂停或转人工；
- 最终由哪个实例处理。

控制台不会要求模型输出冗长私密思维过程，也不会在前端猜测模型没有公开的推理。

## 身份与权限怎么处理

为了方便演示而增加“免登录接口”会直接破坏项目最重要的安全故事。本步继续使用原有 JWT：

| 身份 | Token Scope | 页面能力 |
|---|---|---|
| 普通用户 | `agent:chat` | 提交对话 |
| 退货审批人 | `return:approve` | 批准/拒绝暂停线程 |
| 安全审计员 | `audit:read` | 读取并验证审批哈希链 |

三枚 Token 分别粘贴到密码输入框，只保存在当前 JavaScript 对象和输入框：

- 不使用 localStorage；
- 不使用 sessionStorage；
- 不写 Cookie；
- 不写项目文件；
- 不进入请求 JSON；
- 刷新页面后全部消失。

普通用户 Token 无法审批，审批 Token 无法读取审计链，审计员也无法代替审批人。

## 前端安全边界

页面由 FastAPI 从 Python 包内返回，使用严格响应策略：

```text
Content-Security-Policy:
  default-src 'self'
  script-src 'self'
  style-src 'self'
  connect-src 'self'
  frame-ancestors 'none'
```

- CSS 和 JavaScript 都是同源文件，不加载 CDN；
- 禁止内联脚本，不需要 `unsafe-inline`；
- 禁止第三方网络连接；
- 禁止其他页面通过 iframe 嵌入；
- HTML 使用 `Cache-Control: no-store`；
- 所有用户文本和服务端字段通过 `textContent` 写入，不使用 `innerHTML`。

## 为什么不用 React

当前求职目标是 Agent 后端开发。页面只需要管理一轮对话、三枚内存 Token、一次审批和有限状态，不需要
复杂组件生态或前端路由。原生 HTML/CSS/JavaScript 有三个现实优势：

1. 不增加 Node 构建链和依赖供应链；
2. 静态文件能直接进入当前 Python wheel 与只读 Docker 镜像；
3. 学习和面试重点仍然留在 LangGraph、RAG、工具、审批和工程可靠性。

如果未来团队已经统一 React 技术栈，可以保持 API 契约不变替换展示层。

## 推荐演示顺序

### 1. 订单工具循环

```text
查询订单 SO100001 到哪了
```

观察右侧出现身份校验、分类、规划、工具查询、观察和回答；顶部显示工具次数与实际 A/B 实例。

### 2. 有据 RAG

```text
发票税号写错了怎么办
```

回答下方出现文档编号、标题、版本、生效日期和最高证据分数。时间线显示检索与受约束回答。

### 3. 身份绑定

```text
查询订单 SO200001 到哪了
```

普通用户 Token 的 `sub=user-001`，SO200001 属于 user-002。回答只说未找到或不属于当前用户，不泄漏
真实订单状态。

### 4. Human-in-the-loop

```text
为订单 SO100002 申请退货，原因：商品尺寸不合适
```

后端返回真实 `approval_required` 后出现黄色卡片。此时写工具尚未执行。使用 reviewer Token 批准或拒绝，
页面调用原线程 UUID 恢复 LangGraph；完成后用 auditor Token 验证哈希链。

## 如何运行

先重建并启动包含控制台资源的新镜像：

```powershell
docker compose up --detach --build --wait --wait-timeout 120
```

生成三枚本地短期身份：

```powershell
uv run python examples/09_generate_dev_tokens.py
```

浏览器打开：

```text
http://127.0.0.1:8000/
```

根路径会 307 到 `/console/`。右上角进入“身份与权限”，分别粘贴 customer、reviewer、auditor Token。

自动验证控制台打包、安全 Header、readiness 和本人订单工具：

```powershell
uv run python examples/20_agent_console_smoke_test.py
```

脚本不调用千问、不创建退货、不打印 JWT；报告写入
`data/runtime/agent_console_step20_report.json`。

## 自动验证

新增测试覆盖：

- `/` 和 `/console` 规范重定向；
- HTML、安全响应头、三种密码框无默认 Token；
- CSS/JavaScript 同源可访问且 MIME 正确；
- JavaScript 不调用本地持久化和危险 HTML 插入；
- 控制台不进入 OpenAPI，也没有 demo token/免认证 chat；
- wheel 确实包含 HTML、CSS、JavaScript；
- 容器 CI 通过真实 Nginx 读取页面、CSP 和资源。

## 面试官可能追问

### 为什么页面要求手工粘贴 Token，不做身份下拉框？

身份下拉框如果能直接取得不同角色 Token，本质上就是一个演示认证后门。当前页面演示真实职责分离，
Token 由本地脚本短期签发且刷新即丢。下一步的一键演示可以简化启动和说明，但不会改变后端权限边界。

### 前端时间线可靠吗？

顺序来自后端 `events` 数组，前端只按原顺序映射成易懂标题。工具次数、停止原因和引用使用 ChatResponse
独立字段，不通过页面动画猜测。生产环境可把详细事件限制为内部运维角色，普通用户只看最终状态。

### 页面会不会泄漏 Token？

Nginx 日志不记录 Authorization，页面不持久化 Token，不把它放进 URL、请求体或 DOM 文本，输入框是
password。仍需注意演示现场不要截图或复制到聊天；真正生产应接企业 OIDC/OAuth2，而不是人工粘贴 JWT。

### 两个实例为什么都能提供同一个页面？

控制台文件随同一应用 wheel 打进相同 Docker 镜像，所以 A/B 内容一致。页面请求仍通过 Nginx 轮询；
Checkpoint 与业务状态在共享 PostgreSQL 中，因此一次对话由 A 创建、后续审批落到 B 时仍可恢复。

## 本步文件

- `src/serviceops_agent/web/index.html`：控制台语义结构；
- `src/serviceops_agent/web/assets/console.css`：响应式企业工作台样式；
- `src/serviceops_agent/web/assets/console.js`：同源 API、时间线、审批和审计交互；
- `src/serviceops_agent/api/app.py`：静态挂载、根重定向与 HTML 安全 Header；
- `examples/20_agent_console_smoke_test.py`：页面与真实工具的一键只读冒烟；
- `tests/api/test_console.py`：页面与前端安全契约；
- `.github/workflows/container-image.yml`：最终镜像中的真实页面检查。
