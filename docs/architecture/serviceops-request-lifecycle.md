# ServiceOps Agent 端到端请求生命周期架构图

> 这是适合在 PyCharm 中阅读的分图版本。总览只展示主干，关键参数、异常、审批、数据与评测分别放在后续细图中。
>
> 在 PyCharm 中选择右上角的“编辑器和预览”。每张图都可以单独滚动查看，不需要把整套架构缩成一张小图。

## 1. 一次用户请求的全局总览

```mermaid
flowchart TB
    USER["用户 / 浏览器 / API调用方"] --> NGINX["Nginx统一入口<br/>限流与后端路由"]
    NGINX --> API["FastAPI<br/>参数校验、JWT、Scope、容量保护"]
    API --> NORMALIZE["LangGraph<br/>normalize_request"]
    NORMALIZE --> INTENT["千问意图分类<br/>qwen-plus<br/>confidence阈值 0.85"]

    INTENT --> FAQ["FAQ知识问答<br/>Hybrid RAG + 有据生成"]
    INTENT --> ORDER["订单查询<br/>有限工具Agent循环"]
    INTENT --> RETURN["退货申请<br/>interrupt + 人工审批"]
    INTENT --> HUMAN["人工接管<br/>安全失败出口"]

    FAQ --> RESPONSE["统一ChatResponse"]
    ORDER --> RESPONSE
    RETURN --> RESPONSE
    HUMAN --> RESPONSE
    RESPONSE --> ENDNODE(["返回用户 / 审批人 / 调用方"])

    classDef model fill:#ede9fe,stroke:#6d28d9,color:#172033;
    classDef safe fill:#f1f5f9,stroke:#334155,color:#172033;
    classDef read fill:#ecfeff,stroke:#0e7490,color:#172033;
    classDef write fill:#ffedd5,stroke:#c2410c,color:#172033;
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#172033;
    classDef done fill:#dcfce7,stroke:#15803d,color:#172033;

    class INTENT model;
    class NGINX,API,NORMALIZE safe;
    class FAQ,ORDER read;
    class RETURN write;
    class HUMAN human;
    class RESPONSE,ENDNODE done;
```

## 2. 入口、接口与安全校验

```mermaid
flowchart TB
    USER["用户请求"] --> GATEWAY["Nginx Gateway<br/>5请求/秒 · burst 10<br/>每来源最多20连接"]

    GATEWAY --> RATE{"是否超过入口限制？"}
    RATE -->|"是"| E429["HTTP 429<br/>请求在进入Agent前结束"]
    RATE -->|"否"| INSTANCE["轮询路由<br/>agent-a / agent-b"]

    INSTANCE --> CAPACITY{"单实例是否有Agent工位？<br/>最多8个 · 等待0.05秒"}
    CAPACITY -->|"否"| E503["HTTP 503<br/>服务繁忙"]
    CAPACITY -->|"是"| PYDANTIC["Pydantic请求校验<br/>字段、类型、长度≤4000"]

    PYDANTIC --> VALID{"请求结构是否合法？"}
    VALID -->|"否"| E422["HTTP 422"]
    VALID -->|"是"| JWT["JWT认证<br/>HS256 · iss · aud · exp<br/>时钟误差10秒"]

    JWT --> AUTH{"Token是否可信？"}
    AUTH -->|"否"| E401["HTTP 401"]
    AUTH -->|"是"| RBAC["RBAC / Scope判断"]

    RBAC --> PERMISSION{"是否拥有接口权限？"}
    PERMISSION -->|"否"| E403["HTTP 403"]
    PERMISSION -->|"是"| CONTEXT["创建可信上下文<br/>request_id：服务端生成<br/>thread_id：Checkpoint主键<br/>user_id：JWT sub<br/>idempotency_key：客户端或request_id"]

    CONTEXT --> GRAPH["进入LangGraph"]

    classDef error fill:#fee2e2,stroke:#b91c1c,color:#172033;
    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef safe fill:#eff6ff,stroke:#1d4ed8,color:#172033;
    class E429,E503,E422,E401,E403 error;
    class RATE,CAPACITY,VALID,AUTH,PERMISSION decision;
    class GATEWAY,INSTANCE,PYDANTIC,JWT,RBAC,CONTEXT,GRAPH safe;
```

权限分工：普通客户使用 `agent:chat`；退货审批人使用 `return:approve`；运维人员使用 `operations:reconcile`；审计人员使用 `audit:read`。

## 3. LangGraph控制流与千问意图分类

```mermaid
flowchart TB
    STATE["创建ServiceState<br/>请求、身份、幂等键、事件"] --> NORMALIZE["normalize_request<br/>只清理输入，不做业务判断"]
    NORMALIZE --> QWEN["Qwen Intent Classifier<br/>model=qwen-plus<br/>temperature=0<br/>timeout=30秒<br/>retries=2"]

    QWEN --> OUTPUT["结构化输出<br/>intent + confidence + reason"]
    OUTPUT --> CONF{"输出合法且<br/>confidence ≥ 0.85？"}

    CONF -->|"否"| HUMAN["human_handoff<br/>低置信或模型异常"]
    CONF -->|"是"| ROUTE{"条件边<br/>只允许4种Intent"}

    ROUTE -->|"faq"| FAQ["FAQ路径"]
    ROUTE -->|"order_status"| ORDER["订单路径"]
    ROUTE -->|"return_request"| RETURN["退货路径"]
    ROUTE -->|"human_handoff"| HUMAN

    classDef model fill:#ede9fe,stroke:#6d28d9,color:#172033;
    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#172033;
    classDef flowNode fill:#e0f2fe,stroke:#0369a1,color:#172033;
    class QWEN model;
    class CONF,ROUTE decision;
    class HUMAN human;
    class STATE,NORMALIZE,OUTPUT,FAQ,ORDER,RETURN flowNode;
```

## 4. FAQ：Hybrid RAG只读链路

```mermaid
flowchart TB
    FAQ["FAQ意图"] --> SCOPE{"Scope v2范围门<br/>在Embedding前执行"}
    SCOPE -->|"域外、敏感、内部知识"| HUMAN["转人工<br/>不调用Embedding"]
    SCOPE -->|"公开售后知识范围"| EMBED["Query Embedding<br/>qwen3.7-text-embedding<br/>1024维 · batch≤20"]

    EMBED --> DENSE["Qdrant语义召回<br/>Dense Top-8"]
    EMBED --> BM25["BM25关键词召回<br/>Lexical Top-8"]

    DENSE --> RRF["RRF融合<br/>k=60<br/>Dense权重1.0<br/>BM25权重0.8<br/>最终Top-5"]
    BM25 --> RRF

    RRF --> EVIDENCE{"证据充分？<br/>阈值0.10<br/>published + public"}
    EVIDENCE -->|"否"| HUMAN
    EVIDENCE -->|"是"| GENERATE["Grounded Answer<br/>qwen-plus · Prompt v2<br/>temperature=0<br/>证据≤4000字符"]

    GENERATE --> DRAFT["结构化草稿<br/>answer<br/>citation_ids<br/>is_answerable"]
    DRAFT --> CITATION{"引用安全门<br/>答案非空<br/>is_answerable=true<br/>引用属于本轮白名单"}

    CITATION -->|"不通过"| HUMAN
    CITATION -->|"通过"| ANSWER["返回有据答案和引用"]

    classDef model fill:#ede9fe,stroke:#6d28d9,color:#172033;
    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef read fill:#ecfeff,stroke:#0e7490,color:#172033;
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#172033;
    classDef done fill:#dcfce7,stroke:#15803d,color:#172033;
    class EMBED,GENERATE model;
    class SCOPE,EVIDENCE,CITATION decision;
    class FAQ,DENSE,BM25,RRF,DRAFT read;
    class HUMAN human;
    class ANSWER done;
```

> 当前没有独立 Cross-Encoder Reranker。这里是 Qdrant 与 BM25 双路独立召回，再由 RRF 做排名融合。

## 5. 订单查询：有限工具Agent循环

```mermaid
flowchart TB
    START["订单查询意图"] --> INIT["initialize_order_agent<br/>tool_steps=0<br/>最大实际工具次数=3"]
    INIT --> PLANNER["Qwen Structured Planner<br/>qwen-plus"]
    PLANNER --> ACTION{"有限动作<br/>call_tool / finish<br/>clarify / handoff"}

    ACTION -->|"clarify"| CLARIFY["追问订单号<br/>不调用任何工具"]
    ACTION -->|"finish"| FINAL["代码确定性汇总<br/>不让模型改写订单事实"]
    ACTION -->|"handoff"| HUMAN["转人工"]
    ACTION -->|"call_tool"| GATE{"工具安全门<br/>白名单、参数Schema<br/>JWT身份绑定、去重<br/>工具次数小于3"}

    GATE -->|"不通过"| HUMAN
    GATE -->|"通过"| TOOL["get_order_status<br/>唯一订单只读工具"]
    TOOL --> REPOSITORY["OrderRepository Adapter<br/>检查订单归属"]
    REPOSITORY -.-> OMS["企业OMS / 订单中心<br/>上线前替换<br/>当前为本地JSON"]

    REPOSITORY --> RESULT{"查询结果"}
    RESULT -->|"不存在、越权或异常"| HUMAN
    RESULT -->|"成功"| OBSERVE["ToolObservation<br/>保存结构化业务事实"]
    OBSERVE -->|"仍需查询且未超过3次"| PLANNER

    CLARIFY --> RESPONSE["返回调用方"]
    FINAL --> RESPONSE
    HUMAN --> RESPONSE

    classDef model fill:#ede9fe,stroke:#6d28d9,color:#172033;
    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef read fill:#ecfeff,stroke:#0e7490,color:#172033;
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#172033;
    classDef future fill:#f8fafc,stroke:#64748b,color:#172033,stroke-dasharray:6 4;
    classDef done fill:#dcfce7,stroke:#15803d,color:#172033;
    class PLANNER model;
    class ACTION,GATE,RESULT decision;
    class START,INIT,TOOL,REPOSITORY,OBSERVE read;
    class HUMAN human;
    class OMS future;
    class CLARIFY,FINAL,RESPONSE done;
```

## 6. 退货上半段：资格校验、interrupt与等待审批

```mermaid
flowchart TB
    START["退货申请意图"] --> PREPARE["prepare_return_request<br/>只读资格校验<br/>暂不写业务数据"]
    PREPARE --> CHECK{"信息完整且符合资格？<br/>订单存在、属于当前用户<br/>状态允许退货、原因完整"}

    CHECK -->|"缺少参数"| CLARIFY["返回澄清问题<br/>流程结束"]
    CHECK -->|"明确不符合资格"| REJECT["返回业务拒绝原因<br/>零业务写入"]
    CHECK -->|"订单异常或状态矛盾"| HUMAN["转人工"]
    CHECK -->|"资格通过"| PROPOSAL["ReturnRequestProposal<br/>固定action与参数<br/>绑定可信user_id"]

    PROPOSAL --> INTERRUPT["interrupt<br/>暂停LangGraph"]
    INTERRUPT --> CHECKPOINT[("PostgreSQL Checkpoint<br/>保存工作流位置、草案<br/>可信身份和幂等键")]
    CHECKPOINT --> WAIT["首次HTTP返回<br/>approval_required<br/>thread_id + 审批摘要"]
    WAIT --> REVIEWER["等待人工审批人"]

    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef writeprep fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef pause fill:#fef3c7,stroke:#a16207,color:#172033;
    classDef human fill:#f3f4f6,stroke:#4b5563,color:#172033;
    classDef done fill:#dcfce7,stroke:#15803d,color:#172033;
    class CHECK decision;
    class START,PREPARE,PROPOSAL writeprep;
    class INTERRUPT,CHECKPOINT,WAIT pause;
    class HUMAN human;
    class CLARIFY,REJECT done;
```

## 7. 退货下半段：审批恢复、幂等事务与Outbox

```mermaid
flowchart TB
    REVIEWER["人工审批人"] --> API["POST /api/v1/approvals/{thread_id}"]
    API --> AUTH{"JWT具有<br/>return:approve？"}
    AUTH -->|"否"| E403["HTTP 403"]
    AUTH -->|"是"| THREAD{"线程存在且<br/>正在等待审批？"}
    THREAD -->|"不存在"| E404["HTTP 404"]
    THREAD -->|"不是等待状态"| E409["HTTP 409"]

    THREAD -->|"合法"| DECISION_AUDIT["先记录审批决定<br/>decision_recorded<br/>审批人来自JWT sub"]
    DECISION_AUDIT --> RESUME["Command.resume<br/>使用原thread_id<br/>恢复Checkpoint"]
    RESUME --> APPROVED{"approved？"}

    APPROVED -->|"false"| REJECT["零业务写入<br/>追加rejected审计<br/>返回rejected"]
    APPROVED -->|"true"| IDEMPOTENCY{"幂等写安全门<br/>身份与草案一致<br/>idempotency_key有效"}

    IDEMPOTENCY -->|"冲突或异常"| FAILED["追加failed审计<br/>禁止覆盖原记录"]
    IDEMPOTENCY -->|"通过"| WRITE["execute_return_request<br/>唯一退货写节点"]
    WRITE --> TX["PostgreSQL同一事务<br/>INSERT return_requests<br/>INSERT return_outbox"]
    TX --> RESULT["返回completed<br/>return_request_id"]
    TX --> OUTBOX["Outbox状态 pending"]

    OUTBOX --> RECONCILE["Outbox Reconciler<br/>最多失败3次"]
    RECONCILE --> OUTCOME{"投递结果"}
    OUTCOME -->|"成功"| PROCESSED["processed<br/>追加completed审计"]
    OUTCOME -->|"失败少于3次"| RETRY["退避后重试"]
    RETRY --> RECONCILE
    OUTCOME -->|"失败达到3次"| DEAD["dead_letter<br/>等待人工处理"]

    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef write fill:#ffedd5,stroke:#c2410c,color:#172033;
    classDef audit fill:#fdf4ff,stroke:#a21caf,color:#172033;
    classDef error fill:#fee2e2,stroke:#b91c1c,color:#172033;
    classDef done fill:#dcfce7,stroke:#15803d,color:#172033;
    class AUTH,THREAD,APPROVED,IDEMPOTENCY,OUTCOME decision;
    class API,RESUME,WRITE,TX,OUTBOX,RECONCILE,RETRY write;
    class DECISION_AUDIT audit;
    class E403,E404,E409,FAILED,DEAD error;
    class REJECT,RESULT,PROCESSED done;
```

## 8. 工作流状态、业务事实与审计证据

```mermaid
flowchart TB
    GRAPH["LangGraph节点执行"] --> CHECKPOINT[("工作流状态<br/>Checkpoint / PostgreSQL")]
    CHECKPOINT --> CHECKPOINT_NOTE["像游戏存档<br/>保存走到哪一步和interrupt<br/>不等于真实退货申请"]

    WRITE["批准后的退货写节点"] --> BUSINESS[("业务事实<br/>return_requests<br/>return_outbox")]
    BUSINESS --> BUSINESS_NOTE["像正式业务账本<br/>记录真正发生的退货申请"]

    APPROVAL["审批决定与最终结果"] --> AUDIT[("审计证据<br/>decision / completed<br/>rejected / failed<br/>哈希链")]
    AUDIT --> AUDIT_NOTE["像签字记录和监控录像<br/>证明谁批准了什么<br/>检测记录是否被篡改"]

    KNOWLEDGE["受治理知识源<br/>published + public<br/>切片500 / overlap80"] --> QDRANT[("Qdrant<br/>1024维语义向量")]
    KNOWLEDGE --> BM25["BM25全语料索引"]

    classDef database fill:#eff6ff,stroke:#1d4ed8,color:#172033;
    classDef note fill:#f8fafc,stroke:#64748b,color:#172033;
    classDef source fill:#ecfeff,stroke:#0e7490,color:#172033;
    class CHECKPOINT,BUSINESS,AUDIT,QDRANT database;
    class CHECKPOINT_NOTE,BUSINESS_NOTE,AUDIT_NOTE note;
    class GRAPH,WRITE,APPROVAL,KNOWLEDGE,BM25 source;
```

## 9. 可观测、离线评测和CI治理

```mermaid
flowchart TB
    ONLINE["线上请求链路"] --> OTEL["OpenTelemetry<br/>HTTP / Agent / 节点 / 模型<br/>检索 / 工具 / 数据库 / Outbox Span"]
    OTEL --> SIGNALS["Trace + Metrics + Log<br/>成功率、延迟、异常率<br/>转人工率、容量拒绝、Outbox状态"]
    SIGNALS --> PRIVACY["隐私边界<br/>不记录API Key、JWT原文<br/>用户完整问题和私有证据"]

    CASES["真实业务案例与回归集"] --> EVAL["离线Agent评测<br/>结果 + 过程 + 系统指标"]
    EVAL --> REDLINE{"确定性安全红线通过？"}
    REDLINE -->|"否"| FAIL["直接失败<br/>Judge不能覆盖"]
    REDLINE -->|"是，且唯一失败是<br/>required_fact_missing"| JUDGE["Semantic Judge复核<br/>异常或不确定时失败关闭"]
    EVAL --> ROOTCAUSE["失败归因<br/>模型 / Prompt / 工具 / Workflow"]
    ROOTCAUSE --> CASES

    COMMIT["代码提交"] --> PYTEST["pytest"]
    PYTEST --> RUFF["Ruff"]
    RUFF --> MYPY["Mypy"]
    MYPY --> SPHINX["Sphinx严格构建"]
    SPHINX --> CI["GitHub Actions"]

    classDef observe fill:#f0fdf4,stroke:#15803d,color:#172033;
    classDef decision fill:#fff7ed,stroke:#c2410c,color:#172033;
    classDef error fill:#fee2e2,stroke:#b91c1c,color:#172033;
    classDef quality fill:#eff6ff,stroke:#1d4ed8,color:#172033;
    class ONLINE,OTEL,SIGNALS,PRIVACY,CASES,EVAL,JUDGE,ROOTCAUSE observe;
    class REDLINE decision;
    class FAIL error;
    class COMMIT,PYTEST,RUFF,MYPY,SPHINX,CI quality;
```

评测证据边界：

- 正式密封盲测：`20/30`，成功率 `66.67%`，质量门 `FAIL`，安全红线错误 `0`。
- 揭晓后人工审计：`30/30`，但不是新盲测，不能覆盖正式结果。
- 混合评测器：`30/30 REVEALED_INTEGRATION_REPLAY`，只证明评测器集成逻辑正确。

## 10. 面试讲解顺序

1. 先讲第1张总览，说明项目解决什么问题。
2. 再讲第2、3张，说明请求如何获得可信身份并进入LangGraph。
3. 用第4、5、6、7张分别解释FAQ、订单和退货为什么采用不同控制方式。
4. 用第8张解释Checkpoint、业务事实和审计证据不能混为一谈。
5. 最后用第9张解释线上可观测、离线评测和CI如何形成工程闭环。
