# 2026 年 8 月杭州 AI Agent 岗位要求调研

> 调研日期：2026-08-21
> 目标人群：应届生、实习生和工作年限较少的 AI Agent / 大模型应用开发求职者
> 使用原则：岗位会变化，投递当天仍要重新打开原链接核对是否在招、学历和到岗要求。

## 一、先说结论

对于当前求职目标，最有效的路线不是“学完 LangChain/LangGraph 再投”，而是尽快投递，同时把下面六类
能力做成能运行、能演示、能解释的项目证据：

1. Python 工程基础；
2. FastAPI 接口和基本数据库能力；
3. LangGraph 状态、条件路由、工具循环和人工审批；
4. RAG 的切片、向量检索、引用和无证据降级；
5. 版本化评测集、失败样本分析和回归测试；
6. Docker、日志、鉴权和持久化等最小工程化能力。

一句通俗的比喻：招聘方不是在找“背过所有汽车零件名称的人”，而是在找“能把车开起来、知道刹车为何
安全、出故障能定位问题的人”。框架知识要学，但应该围绕一个完整项目反复理解，而不是与项目割裂。

## 二、本次岗位样本

| 样本 | 层级与地点 | JD 明确强调的能力 | 对当前求职的含义 |
|---|---|---|---|
| [恒生电子 AI 应用开发工程师](https://www.nowcoder.com/jobs/detail/456959) | 校招，杭州，本科及以上 | Python/Java、FastAPI、SQL、LLM API、Prompt、RAG、LangChain、Git/Linux | 与本项目最匹配，应该优先投递类似岗位 |
| [海康威视大模型应用开发工程师](https://www.nowcoder.com/jobs/detail/432802) | 实习，杭州，在校生 | Python、FastAPI/Streamlit、千问等 API、LangChain/LlamaIndex、RAG/Agent、个人项目 | 说明完整个人项目本身就是有效敲门砖 |
| [杭州个性进化 AI 技术开发实习生](https://www.zhaopin.com/jobdetail/CCL1509703210J40841407307.htm) | 实习，杭州，本科 | FastAPI/Flask、RAG、Agent、Prompt、日志、调试、Git、完整小项目 | 初创团队很看重“能把模糊需求变成跑得起来的代码” |
| [AI 大模型开发管培生](https://www.shushuqiuzhi.com/position/363602) | 2026 校招，杭州，硕士 | Python、工具调用、任务规划、RAG、FastAPI/Pydantic、SQL、LangChain/Dify、Docker | 说明 Agent 控制流、RAG 和后端要一起准备 |
| [Agent 应用开发工程师](https://www.shushuqiuzhi.com/position/212884) | 2026 校招，杭州，本科 | Python、LangChain/LangGraph、RAG、工具使用、多步工作流、Agent 评测、FastAPI、Docker/Git | 与本项目覆盖面高度一致，但该页面显示已截止，只用于技能研究 |
| [海康威视大模型 RAG 算法实习生](https://talent.hikvision.com/home/socity/position?postId=062541642F1585B48071F76C89E1B801) | 实习，杭州 | 向量数据库、检索、Embedding、Rerank、LLM | 如果专投 RAG 岗，需要补一轮混合检索和重排实验 |
| [海康威视高级大模型应用开发](https://talent.hikvision.com/home/socity/position?postId=B4F6AAF8C5C1FEB7D6C131231EBAB46F) | 社招资深，杭州 | 端到端 Agent、LangGraph、RAG、Function Call、评测、可观测、安全、Docker/K8s、CI/CD | 不能把它当当前学历/年限门槛，但可当两三年后的能力地图 |
| [海康威视大模型应用算法工程师](https://talent.hikvision.com/home/socity/position?postId=F3572C5AD0A1263CE644BEF27E2AA66F) | 社招算法，杭州 | Agent 架构、准确率/延迟/稳定性/成本、自动评测、Bad Case 闭环、Qwen/DeepSeek/GPT | 提醒项目不能只展示“能回答”，还要展示“如何评估和修复” |

说明：前五项来自可访问的招聘聚合页或招聘平台，第六至八项来自海康威视招聘页。聚合页信息可能过期，
因此本表用于归纳技能，不等同于职位仍开放。投递状态必须以公司官网或招聘方当天页面为准。

## 三、六份应届/实习样本的高频项

下面的次数是对上表前六份应届/实习 JD 的人工编码，只表示“这六份 JD 是否明确写到”，不是对杭州全部
岗位的统计学结论。

| 能力类别 | 明确提及的样本数 | 当前项目状态 | 现在是否继续投入 |
|---|---:|---|---|
| RAG / 检索 / 向量数据库 | 6/6 | 已有切片、Qdrant、证据阈值、引用和 RAG 评测 | 保持，会讲清 Recall@K、MRR 与无证据降级 |
| Python | 5/6 | Python 3.12，严格类型和自动测试 | 保持，同时补数据结构与异步基础 |
| FastAPI / Web 后端 | 5/6 | 已有 API、Pydantic、JWT、健康检查和控制台 | 保持，重点理解请求生命周期和异常处理 |
| Agent / 工作流 / 工具调用 | 5/6 | 已有状态图、条件路由、工具循环、interrupt | 核心卖点，必须能白板解释 |
| LangChain / LangGraph 等框架 | 4/6 | LangGraph 1.x + LangChain Tools | 不横向再学多个框架，先把当前代码吃透 |
| LLM API / Prompt / 模型基础 | 5/6 | 已接入 `qwen-plus` OpenAI 兼容接口 | 保持，补 token、temperature、结构化输出、超时重试 |
| SQL / Git / Docker / 工程实践 | 4/6 | PostgreSQL、GitHub Actions、Docker Compose、Nginx | 是区别于普通聊天 Demo 的加分项 |
| 个人项目 / 开源作品 | 3/6 | 已有完整项目与一键演示，但尚需公开 GitHub | 尽快整理提交历史并公开可展示版本 |
| Agent 评测 | 1/6 明确点名，多份隐含要求测试/稳定性 | 已有四维评测、候选实验和质量门 | 属于稀缺亮点，简历应写但必须注明样本范围 |

## 四、该投什么岗位

### 第一优先级：现在就投

- AI Agent 应用开发工程师；
- 大模型应用开发工程师；
- LLM 应用 / RAG 开发工程师；
- Python AI 后端工程师；
- 大模型应用实习生、可转正实习生。

这些岗位与项目的交集最大。即使 JD 写了“熟悉 LangChain”，也不代表必须把 LangChain 每个模块都背完；
只要能说明为何用 LangGraph 管控制流、为何工具权限不能交给模型、如何评测失败样本，就有实质项目证据。

### 第二优先级：看 JD 再投

- RAG 算法/检索工程师：需要额外补 BM25 + 向量混合检索、Reranker 对照实验；
- Agent 平台工程师：需要更深的异步、消息队列、缓存、租户隔离和平台部署知识；
- AI 后端工程师：要把 Python、网络、数据库、并发和 Linux 基础放到简历更靠前。

### 当前不要作为主攻

- 大模型训练、SFT、RLHF/GRPO、分布式训练岗位；
- 以论文、模型算法创新为核心的研究岗；
- 明确要求三年以上生产经验或主导过大规模平台的资深岗。

不是永远不学，而是这些方向短期投入很难转化为当前可验证的求职优势。

## 五、项目覆盖与真实缺口

### 已经形成证据

- LangGraph 状态、条件边、工具循环、Checkpoint、`interrupt` 和 `Command.resume`；
- Qdrant RAG、引用约束、无证据转人工；
- 千问 API、Pydantic 结构化输出和模型故障降级；
- FastAPI、PostgreSQL、SQLite、JWT/RBAC、Outbox 和审计；
- Docker Compose、Nginx 双实例、限流、健康检查、备份恢复；
- 版本化 Agent 评测、真实千问重复实验、Pytest/Ruff/Mypy/CI；
- 可视化控制台、Checkpoint 回放和五分钟面试演示。

### 投递前仍要补的不是“大模块”，而是本人理解

1. 能从 `ServiceState` 开始口述一次订单请求如何经过节点和条件边；
2. 能说明模型为什么不能决定用户身份、工具权限和是否落库；
3. 能说明 RAG 检索指标与最终回答正确率不是同一个指标；
4. 能解释为什么 13/13 只证明这份小型回归集通过，不代表所有用户问题都正确；
5. 能在 PyCharm 断点观察 State 更新、interrupt 暂停和恢复；
6. 能独立修改一个小需求并补测试，避免面试官判断项目只是“会运行但不会改”。

### 以后再补的技术

- MCP 工具协议；
- BM25 + 向量混合检索和 Reranker；
- Redis / 消息队列；
- 云端 TLS、OIDC/JWKS、Kubernetes；
- vLLM 本地模型服务与成本/吞吐对照。

这些是加分路线，不应该阻塞本周投递。
