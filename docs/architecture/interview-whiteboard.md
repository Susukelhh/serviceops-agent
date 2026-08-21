# ServiceOps Agent 面试白板画法

目标：不用看代码，在 90 秒内画出面试官能继续追问的架构，而不是画满所有类名。

## 一、先画五个横向盒子

```text
[用户/五角色] → [Nginx + FastAPI + JWT] → [LangGraph] → [工具/RAG/人工] → [PostgreSQL]
```

每个盒子只说一句：

1. **用户/五角色**：对话、审批、审计、调试和运维权限彼此分离。
2. **入口**：限流、参数校验、身份校验都发生在模型和工具之前。
3. **LangGraph**：保存 State，用条件边、环和 interrupt 控制下一步。
4. **执行层**：模型只建议，白名单工具和人工决定才有行动权。
5. **数据层**：Checkpoint、业务事实、审计证据职责分离但共享 PostgreSQL。

## 二、再从 LangGraph 画三条分支

```text
                    ┌→ FAQ：范围门 → Qdrant Top-5 → BM25重排 → 引用回答
[LangGraph 分类] ───┼→ 订单：规划 → 工具 → 观察 ↺
                    └→ 退货：草案 → interrupt → 审批 → resume → 幂等写入
```

重点解释两个形状：

- `↺` 表示 Agent 不是一次函数调用，而是受最大步数和重复检测保护的循环；
- `interrupt` 表示图保存进度后主动暂停，不是程序崩溃。

## 三、最后补上两条虚线治理边

```text
[LangGraph / API] - - → [OpenTelemetry]
[黄金集 / CI]    - - → [质量门]
```

然后主动说出边界：本地 Compose 已验证双实例恢复、限流和备份，但没有冒充 TLS、云数据库高可用、
企业 OIDC、PITR 或 Kubernetes 已经完成。

## 四、面试官沿盒子追问时怎么进入代码

| 追问 | 先打开的代码 |
|---|---|
| State、节点、条件边怎么写 | `src/serviceops_agent/graph/state.py`、`graph/builder.py` |
| 工具怎么防止模型越权 | `tools/order.py`、`graph/nodes/order.py` |
| interrupt 如何恢复 | `graph/nodes/returns.py`、`api/app.py` |
| Checkpoint 在哪里装配 | `infrastructure/runtime.py` |
| PostgreSQL 幂等和事务 | `infrastructure/postgres_repository.py` |
| JWT/RBAC 怎么做 | `security/jwt_auth.py`、`security/models.py` |
| 怎么证明不是只跑一个样例 | `evaluation/`、`data/evaluation/` |
| Docker 双实例怎么组织 | `compose.yaml`、`deploy/nginx/nginx.conf` |

## 五、练习标准

- 90 秒内画完五盒、三分支、两条治理边；
- 不看讲稿说清 Checkpoint 与业务表的区别；
- 主动指出至少两个生产差距；
- 面试官点任一盒子时，能打开对应目录而不是全局搜索。
