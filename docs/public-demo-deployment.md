# ServiceOps Agent 公网作品演示

## 访客会看到什么

打开首页后，页面会自动申请一枚十分钟沙盒身份，不要求访客复制 JWT。左侧四个场景分别展示：

1. 查询本人样例订单，观察 Agent 规划与工具调用；
2. 查询售后政策，观察 Qdrant、BM25、RRF、引用和有据回答；
3. 创建退货草案，观察 LangGraph `interrupt`、人工审批、恢复执行和审计链；
4. 请求内部风控阈值和系统提示词，观察范围识别与安全转人工。

每次问答后，页面会自动读取属于本次沙盒身份的脱敏 Checkpoint。访客可以逐步查看 State
变化、执行节点、工具结果、RAG 引用和审批状态，但看不到模型隐藏思维过程。

## 它为什么不是“裸奔接口”

- `POST /api/v1/demo/session` 只在 `SERVICEOPS_PUBLIC_DEMO_ENABLED=true` 时存在；
- 返回的 JWT 只存当前页面内存，默认十分钟过期，响应禁止缓存；
- 每名访客使用独立 `demo-...` 身份，审批、审计和调试接口都会复查线程归属；
- 访客只能访问专门准备的样例订单，不能映射到其他用户数据；
- 匿名消息最多 500 字，Nginx 和 FastAPI 还有速率、连接数和并发工位限制；
- 默认 `mock + hash + extractive`，不会消耗千问余额；若误配真实模型，应用会拒绝启动。

## 先在本机验证

```powershell
cd D:\serviceops-agent
docker compose up --build -d
docker compose ps
```

浏览器打开 `http://127.0.0.1:8000/`。看到“公网安全沙盒已就绪”后，依次运行四个场景。
第三个场景应先显示“写操作尚未执行”，点击批准后才产生退货申请和审批证据链。

验证完停止服务：

```powershell
docker compose down
```

## 真正放到公网前

项目当前提供的是可部署应用，不代替云账号、域名和 HTTPS 证书。云服务器至少需要完成：

1. 只开放 80/443，数据库、Qdrant 和 Agent 容器端口不对公网暴露；
2. 使用云负载均衡、Caddy、Traefik 或 Cloudflare 在最外层终止 HTTPS；
3. 注入独立强 JWT 密钥和 PostgreSQL 密码，不提交 `.env`；
4. 把网关监听地址改成 `0.0.0.0`，并开启 production 环境；
5. 保持付费模型开关为 false，先用零费用模式观察访问量。

服务器 `.env` 的关键配置示例：

```dotenv
SERVICEOPS_ENVIRONMENT=production
SERVICEOPS_GATEWAY_BIND_ADDRESS=0.0.0.0
SERVICEOPS_PUBLIC_DEMO_ENABLED=true
SERVICEOPS_PUBLIC_DEMO_ALLOW_PAID_MODEL=false
SERVICEOPS_JWT_SECRET_KEY=请使用密码管理器生成至少32字符的随机值
SERVICEOPS_POSTGRES_PASSWORD=请使用另一个随机强密码
```

公网验收时，应检查 HTTPS、四个场景、会话到期、跨会话 404、429 限流、容器重启后的
Checkpoint 恢复，以及日志中没有 Authorization Header、问题正文和密钥。
