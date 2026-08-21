# 第九步：JWT 身份认证与 Scope 权限隔离

## 本步解决的问题

第八步之前，HTTP 请求体可以提交 `user_id`，审批请求也可以提交 `reviewer_id`。即使模型看不到
身份参数，攻击者仍可能直接修改 JSON 冒充其他用户或审批人。这属于 API 认证边界缺失，不能靠
Prompt、LangGraph State 或工具 Schema 修复。

第九步将身份链路改为：

```text
Authorization: Bearer <JWT>
→ 固定算法 + 签名 + iss/aud/iat/exp/jti 校验
→ Pydantic Claims 校验
→ 角色—Scope 策略校验
→ FastAPI 路径 Scope 校验
→ Token sub 注入 user_id / reviewer_id
→ 才允许进入 LangGraph
```

请求体已经删除 `user_id/reviewer_id`，并使用 `extra="forbid"`。即使 Token 合法，只要 JSON
重新夹带身份字段也会返回 422。

## 认证和授权的区别

- 认证 Authentication：这枚 Token 是否由可信签发方签名，当前主体是谁；
- 授权 Authorization：该主体能否调用当前接口。

本项目用 JWT `sub` 表达认证身份，用角色映射的 Scope 表达授权：

| 角色 | Scope | 允许接口 |
|---|---|---|
| `customer` | `agent:chat` | `POST /api/v1/chat` |
| `return_reviewer` | `return:approve` | `POST /api/v1/approvals/{thread_id}` |
| `auditor` | `audit:read` | `GET /api/v1/audit/approvals/{thread_id}` |

审批人默认不能查询普通用户订单或读取审计记录，普通用户也不能批准自己的退货申请。审计员只能
读取最小化证据链。身份通过但缺少 Scope 返回 403；缺 Token、签名错误、过期或 Claims 无效
返回 401。

FastAPI 官方支持在安全依赖中传递 Scope，并将其作为细粒度权限使用：

- [FastAPI OAuth2 Scopes 官方文档](https://fastapi.tiangolo.com/advanced/security/oauth2-scopes/)
- [FastAPI JWT 官方教程](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)

当前项目使用 HTTP Bearer 方便 Swagger 直接粘贴开发 Token，尚未实现 OAuth2 登录/授权服务器。

## JWT 必须校验什么

`decode_access_token` 不只是把 Base64 Payload 解出来，而是强制验证：

- 固定 `HS256` 白名单，绝不根据攻击者可控 Header 动态选择算法；
- 签名必须匹配 `SecretStr` 密钥；
- `iss` 必须等于配置签发方；
- `aud` 必须等于当前 Agent API；
- `iat`、`exp` 必须存在，过期 Token 拒绝；
- `sub` 是唯一业务身份来源；
- `jti` 必须存在，为后续撤销和审计预留；
- `roles` 必须是有限角色；
- `scope` 必须是有限权限；
- Token Scope 不能超出服务端角色策略。

PyJWT 官方建议显式提供允许算法，并可以使用 `options={"require": [...]}` 要求关键 Claims
存在：[PyJWT Usage](https://pyjwt.readthedocs.io/en/latest/usage.html)。

所有认证失败统一返回“无效或已过期的访问令牌”，不会告诉攻击者是密钥、audience、过期时间还是
角色组合接近正确。服务端异常链可用于调试，但 Token 原文和密钥不能写日志。

## 身份如何进入 Agent

对话接口现在接收：

```json
{
  "message": "查询订单 SO100001 到哪了"
}
```

FastAPI 安全依赖返回 `AuthenticatedPrincipal`，路由使用：

```python
"user_id": principal.subject
```

后续订单 Tool 仍从可信 State 创建身份绑定闭包，模型只提供订单号。安全链路由此前的“请求体 →
State → Tool”升级为“签名 JWT sub → State → Tool”。

审批接口请求体现在只有：

```json
{
  "approved": true,
  "comment": "订单已核验"
}
```

`reviewer_id` 使用具有 `return:approve` 的 Token `sub`。resume 仍不能覆盖原申请用户、订单、原因
或幂等键。

## 本地开发密钥与生产身份平台

为了让首次 PyCharm 运行不依赖外部身份平台，development 环境提供明确标记的本地占位密钥。
它不是生产秘密。`Settings` 会：

- 拒绝少于 32 字符的 HS256 密钥；
- 在 `environment=production` 时拒绝仓库内开发密钥和明显占位值；
- 使用 `SecretStr` 防止配置 repr 直接显示明文。

真实企业部署不应该长期共用本项目 HS256 密钥。应对接企业 OIDC/OAuth2 身份平台，使用 JWKS
验证非对称签名，并设计 `kid` 密钥轮换、Token 撤销、账号禁用和组织/租户 Claims。

## 在 PyCharm 中使用

先启动 API，然后右键运行：

```text
examples/09_generate_dev_tokens.py
```

脚本输出：

- `user-001` 普通用户 Token；
- `reviewer-001` 退货审批 Token；
- `auditor-001` 审计读取 Token；
- 预计 UTC 过期时间。

打开 Swagger：

```text
http://127.0.0.1:8000/docs
```

点击 **Authorize**，只粘贴 Token 本身。查询/发起退货使用 customer Token；批准或拒绝时重新
Authorize 为 reviewer Token。Token 不写文件、不打印签名密钥，默认三十分钟后失效。

## 本步测试

完成第十步后当前共 95 个测试；本步认证重点包括：

- 合法 customer Token 解码为可信主体；
- 普通用户只有 `agent:chat`，没有 `return:approve`；
- 过期 Token 返回统一 401；
- 其他 audience 的合法 Token 也被拒绝；
- 签发端不能创建角色策略之外的 Scope；
- 解码端再次拒绝“签名正确但角色—Scope 冲突”的 Token；
- production 拒绝仓库开发密钥；
- 缺 Bearer Token 的 chat 返回 401；
- 请求体伪造 `user_id` 返回 422；
- customer 越权审批返回 403，interrupt 仍可由 reviewer 正常处理；
- 原有持久化、审批、Agent、RAG 和故障回归全部通过。

## 面试追问与回答方向

- JWT Payload 能直接信任吗？
  不能。JWT Payload 只是编码；必须验证签名、固定算法、标准 Claims、结构和业务权限策略。
- 为什么 user_id 不能留在请求体再和 Token 对比？
  没必要增加两个身份来源。业务身份只取 sub，可以消除不一致和遗漏比较的风险。
- 401 和 403 有什么区别？
  401 表示未通过认证；403 表示身份已确认但没有当前操作权限。
- 为什么验证 aud？
  防止其他内部服务签发的 Token 被横向拿到 Agent API 使用。
- 为什么固定 algorithms？
  防止攻击者通过 JWT Header 影响验证方式，造成算法混淆。
- Scope 和角色为什么都要有？
  角色便于组织管理，Scope 适合接口细粒度授权；服务端还应检查二者组合是否符合策略。
- 当前为什么仍不算完整生产认证？
  项目只实现资源服务器验证切片，没有 OIDC 登录、JWKS、轮换、撤销、租户和账号目录。
- Agent 身份安全与 Prompt 有关系吗？
  没有。认证必须在模型调用前由 HTTP 安全边界完成，Prompt 不能充当权限系统。
