# Teaching Notes

- 使用中文解释，先给结论，再解释技术取舍。
- 每个 LangGraph 概念尽量映射到企业工单 Agent 的真实节点。
- 课程学习与项目实现并行；避免长时间只看视频不写代码。
- 所有简历指标必须来自真实评测或压测，禁止编造。
- 项目固定放在 `D:\serviceops-agent`，后续代码、文档、课程和评测资料统一归档。
- 用户希望代码包含较详细的中文模块说明、函数说明和关键逻辑注释。
- 每完成一个实施步骤，都要总结本步完成内容、使用的技术栈和下一步目标。
- 所有 Python 代码采用教学级中文注释：除模块和函数说明外，还要逐字段说明 State、
  Pydantic Schema、配置项和枚举的含义、来源、写入方与消费方。
- 每段关键代码和关键语句都要解释设计意图、输入、输出或副作用；不能只在文件顶部概述。
- 用户当前仍在建立 Docker、数据库、消息队列等工程基础概念，默认避免直接堆叠专业名词。
- 以后讲解新技术统一采用“现实问题 → 生活类比 → 项目中的作用 → 专业术语 → 可动手验证”顺序。
- 首次出现专业名词时必须立刻给出一句白话定义；能用快递、餐厅、档案柜、流水线等浅显例子时优先举例。
- 区分“当前求职阶段必须掌握”和“暂时只需听过”的内容，避免用户因细节过多失去主线。

## 2026-08-21：第十七步完成，核心项目 v1 工程闭环

- 业务表结构改由 Alembic `20260821_0001` 管理；API 不再在启动期执行项目业务 DDL。
- Compose 升级为 Nginx gateway、agent-a、agent-b、一次性 migrate、共享 PostgreSQL 五角色。
- Windows 只暴露 `127.0.0.1:8000`；两只 Agent 无宿主机端口，并保持非 root、只读根目录和零 Capability。
- 连续健康请求实际观察到 B/A/B/A/B/A 轮询；网关和实例响应头可用于低敏定位。
- 自动演练真实完成 A 创建 interrupt、停 A、B 恢复审批、审计链验证和双实例恢复。
- 两个独立 PostgreSQL 连接池并发提交相同幂等键：一条首次创建、一条安全重放，总数只增加一。
- 第十六步旧线程/退货/审计数据在迁移和拓扑重建后仍能读取，证明升级没有删除具名卷数据。
- 本机最终门禁：Ruff PASS、Mypy 67 个源码文件 PASS、Pytest 137 passed/2 skipped、Agent Eval 13/13。
- PostgreSQL 专用临时库测试 2/2 PASS；容器 CI 已同步为真实五角色 Compose 验证。
- 核心项目 v1 已覆盖可运行、可评测、可部署、可恢复和多实例协作；后续优先转向演示、面试表达和简历，而不是继续堆 Agent 框架。

## 2026-08-21：第十八步容量保护与恢复基线完成

- Nginx 只对 `/api/` 应用每来源 5r/s、burst 10 和最多 20 个同时处理连接；健康、就绪和 Swagger 不限流。
- 入口超限返回固定 JSON 429、`Retry-After: 1` 和过载类型 Header；日志记录 `rate_limit=REJECTED`，不记录 Token/正文。
- 每只 Agent 使用 8 个 `asyncio.BoundedSemaphore` 业务工位，排队 0.05 秒仍无容量则返回脱敏 503。
- 后端容量拒绝写入 `serviceops.http.capacity.rejections`，指标只使用固定 `queue_timeout` 标签。
- 本机离线正常阶段 12/12 成功，agent-a/agent-b 各 6 条，P50 48.65 ms、P95 60.65 ms。
- 40 条瞬时请求中 11 条成功、29 条被 429 削峰；无 500/502/504/连接失败；三秒后恢复 200 和 readiness 4/4。
- 上述延迟只可描述为本机 Docker + 离线确定性后端基线，禁止冒充真实千问 SLA/QPS。
- 容器 CI 已增加真实突发 429、无 5xx 和等待后恢复 401 的验证步骤。

## 2026-08-21：第十九步 PostgreSQL 备份与隔离恢复完成

- 新增 `operations/postgres_backup.py`，通过 Compose 内网调用容器自带的 pg_dump/pg_restore，不暴露 5432 或密码。
- custom 归档先写 `.partial`，成功后原子改名；清单记录 SHA-256、PostgreSQL 版本、大小与有限表指纹。
- 恢复目标只能是代码内部生成的 `serviceops_restore_drill_<12位随机值>`，真实 `serviceops` 永远被保护器拒绝。
- pg_restore 使用单事务与遇错即停；恢复后逐表比较表集合、行数和内容哈希，finally 删除临时库。
- 本机真实归档 160047 bytes、36 个归档对象、8 张公共表一致，SHA-256 为 `bf8f22034e3f850a5cfbba71cc00755e4efcb0c5250905e7334e2864e400463a`。
- 数据库查询确认没有 `serviceops_restore_drill_%` 临时库残留；备份目录同时排除 Git 与 Docker build context。
- 单元测试覆盖危险库名拒绝、成功清单、指纹不一致仍清理和敏感目录忽略；容器 CI 增加真实恢复演练。
- pg_dump 逻辑备份不等同高可用、异地副本或 PITR；生产仍需云数据库自动备份、WAL 归档和定期恢复演练。

## 2026-08-21：第二十步可解释 Agent 运行控制台完成

- 服务根路径 307 进入 `/console/`；HTML、CSS、JavaScript 位于 `serviceops_agent/web` 并真实进入 wheel。
- 左侧 readiness 展示 API、Checkpointer、退货库、Outbox 和审计库；中间是真实 Agent 对话与 RAG 引用。
- 右侧把公开 events 映射为认证、分类、检索、规划、工具、审批和响应时间线，不展示模型隐藏思维链。
- 退货响应真实返回 interrupt 后才出现审批卡；批准/拒绝使用 reviewer Token 恢复原 thread_id。
- 审批完成后使用独立 auditor Token 读取最小哈希链，普通用户和审批身份不能替代审计员。
- Token 只保存在当前 JS 内存/密码输入框，不调用 localStorage/sessionStorage，不增加 demo token 或免认证 chat。
- 所有动态文本使用 textContent，不使用 innerHTML；HTML 设置同源 CSP、no-store、nosniff、DENY frame。
- 新增 6 项控制台接口/安全测试，容器 CI 检查页面、CSP 与同源静态资源确实进入最终镜像。
- 新镜像真实冒烟通过：页面、CSS/JS、CSP、readiness 4/4、Nginx 和 `get_order_status` 均 PASS，本轮落到 agent-b。
- 最终本地门禁：Ruff PASS、Mypy 70 个源码文件 PASS、Pytest 150 passed/2 skipped、Agent Eval 13/13。

## 2026-08-21：第 20.1 步 Checkpoint 教学调试模式

- 新增独立 `developer` 角色与 `debug:read`，不能继承对话、审批、审计或运维权限。
- `GET /api/v1/debug/threads/{thread_id}` 只在 development/test 可用；production 固定返回 404。
- 后端通过 LangGraph `aget_state_history` 读取 `StateSnapshot`，不直接查询 Checkpointer 私有数据库表。
- 历史从“最新优先”反转为时间正序，并根据上一快照 `next` 标记刚执行节点，计算公开字段增删改。
- 状态采用字段白名单；用户/审批身份、Token jti、幂等键、工具指纹、Outbox 编号和异常原文不返回。
- Checkpoint Serializer 显式允许六个项目领域类型且不启用 pickle fallback，避免任意类型反序列化。
- 前端支持上一步/下一步、快照步骤条，以及状态变化、当前状态、工具/RAG、安全审批、Checkpoint 五视图。
- interrupt 使用橙色步骤显示；批准/拒绝后自动重新读取同一 thread_id，把恢复快照接在暂停历史之后。
- 新增第十三课与 Checkpoint 速查页，术语采用“工程执行轨迹”，明确不暴露模型隐藏推理原文。
- Docker 真实冒烟验证通过：一次订单查询生成 9 个时间正序 Checkpoint，并识别出 `execute_order_tool` 工具节点。
- 请求经 Nginx 入口落到 agent-a，调试历史由另一实例 agent-b 从共享 PostgreSQL 成功读取，证明不是单进程内存假象。
- 本步最终门禁：Ruff PASS、Mypy 72 个源码文件 PASS、Pytest 156 passed/2 skipped、离线 Agent Eval 13/13；容器日志无 Traceback 或 ERROR。

## 2026-08-21：第 20.2 步全宽教学调试工作台

- Checkpoint 播放器从右侧证据窄栏移到对话区下方，和顶部内容使用相同的 1520px 最大宽度。
- 普通模式采用“流程解释 360px + 状态检查器自适应”布局，详情区默认最小 570px 高并支持双列状态卡。
- 新增专注大屏：隐藏侧栏、对话和指标，调试工作台固定占满浏览器；再次点击或按 `Esc` 可退出。
- 大屏模式支持左右方向键切换 Checkpoint；输入框获得焦点时不拦截方向键，避免破坏文字编辑。
- 模式切换只修改当前浏览器 DOM/CSS，不重新请求模型、不修改 Agent State，也不扩大 developer 权限。
- HTML、CSS、JavaScript 与容器 CI 增加全宽工作台、大屏按钮、键盘退出和打包资源契约检查。
- 新 Docker 镜像真实冒烟通过：页面资源、专注大屏契约、订单工具和 9 个 Checkpoint 回放均 PASS，本轮请求落到 agent-a。
- 本步最终门禁：JavaScript 语法 PASS、Ruff PASS、Mypy 72 个源码文件 PASS、Pytest 156 passed/2 skipped；四个容器全部 healthy。

## 2026-08-21：第 21 步一键面试演示与证据化讲解

- 新增 `serviceops_agent.demo.interview_demo`，按 Compose 校验、健康启动、真实冒烟、短期身份和本地页面顺序准备演示。
- Docker 解析优先 PATH，再检查用户级与全局 Docker Desktop 固定位置；不递归扫描磁盘或自动下载安装。
- 所有子进程使用参数数组且固定项目工作目录，不通过 shell 拼接；失败后立即停止，不继续输出 Token 或打开页面。
- 默认 Compose 固定 mock、确定性规划、Hash Embedding 与抽取回答，因此预检不调用千问、不自动批准退货。
- 报告只保存步骤结论和布尔边界，不保存 docker.exe 本机路径、Token、API Key 或用户消息。
- 新增五分钟演示 Runbook、故障排查表、第14课、一页式速查卡和第4份学习记录。
- 本机真实快速预检两次 PASS：Compose 配置、五角色健康等待、真实订单工具和 9 个 Checkpoint 回放全部通过；最近请求落到 agent-a。
- 最终报告明确 `tokens_persisted_by_launcher=false`、`paid_model_called=false`，且没有保存 Docker 本机路径。
- 本步最终门禁：Ruff PASS、Mypy 74 个源码文件 PASS、Pytest 159 passed/2 skipped、依赖锁 PASS、wheel 构建与内容检查 PASS。

## 2026-08-21：第 22 步三张架构图与面试白板表达

- 删除原有一张超长调用链和已经过期的“未实现限流/迁移/备份”等历史描述，重建唯一权威架构文档。
- 系统总览按角色入口、接入安全、LangGraph、确定性执行数据、工程治理五层组织，避免堆满类名。
- 退货时序明确审批前零业务写入、decision 先记录、Command.resume、业务+Outbox 单事务和审计投递。
- Docker 拓扑明确 Windows 只访问 Nginx、双 Agent 无宿主端口、migrate 先行、PostgreSQL 不映射 5432。
- README 增加小型求职展示图；另有 90 秒白板画法、第15课、可打印架构速查图和第5份学习记录。
- 新增架构契约测试，防止三张图、关键组件、诚实生产差距和已完成能力在后续修改中再次漂移。
- 第22步最终门禁：架构专项 3/3 PASS、Ruff PASS、Mypy 74 个源码文件 PASS、Pytest 162 passed/2 skipped、依赖锁 PASS。

## 2026-08-21：第 23 步杭州岗位调研与简历证据

- 调研杭州六份应届/实习和两份资深 Agent/大模型应用岗位，区分当前投递门槛与未来能力地图。
- 高频主线确定为 Python、FastAPI、Agent 工作流/工具、RAG、LLM API 和基础工程实践，不等待学完框架再投。
- 新增一页式中文简历内容初稿；姓名、学校、联系方式、毕业时间和其他经历全部保留占位符，不编造。
- 把 LangGraph、RAG、HITL、Outbox、评测、PostgreSQL、Docker 和可观测描述映射到具体代码与追问题。
- 简历明确 13/13 是 13 条小型黄金集；千问 1.1.0 三轮各 13/13，不外推为线上全量准确率。
- 明确项目是生产导向个人项目，不冒充企业上线、线上 QPS、Kubernetes、MCP、Redis、Reranker 或模型微调。
- 新增求职材料契约测试；最终门禁为专项 3/3、Ruff PASS、Mypy 74 个源码文件 PASS、Pytest 165 passed/2 skipped、离线 Agent Eval 13/13、依赖锁 PASS。

## 2026-08-21：第 24 步问题驱动 RAG 困难基线

- 将 RAG 评测从 4 个可索引短文、11 条简单题扩展为 14 份原始文档、12 份活动公共文档、23 个真实 Chunk。
- 新建 32 条开发样本和 12 条锁定样本，覆盖口语改写、相邻政策、跨段问题、域外高词面重合、内部规则和退役规则。
- Baseline 固定 Hash Embedding 1024 维、字符窗口 500/Overlap 80、Top-5、阈值 0.10，不调用付费 API。
- 开发集结果为 Recall@5 100%、MRR@5 93.1%、Top-1 87.5%、nDCG@5 94.8%、负例误召回率 100%。
- 失败诊断为 8 条负例误召回和 3 条正确文档排序靠后；说明后续要分别验证拒答边界与 Rerank，而不是堆技术。
- 内部草稿与退役政策在所有查询结果中保持不可见；holdout 本阶段只校验存在和数量，没有参与调参。
- 新增第 16 课、RAG 实验卡、完整实施记录与第 6 份学习记录；最终门禁为 Ruff PASS、Mypy 75 个源码文件 PASS、Pytest 168 passed/2 skipped、离线 Agent Eval 13/13、依赖锁 PASS。

## 2026-08-21：第 25 步 RAG 阈值取舍与业务范围门

- 以 0.10、0.15、0.20、0.25、0.30、0.35 六档执行阈值单变量扫描，证明降低误召回会快速损失正例 Recall。
- 阈值 0.30 的负例误召回率降到 0%，但 Recall@5 从 100% 跌至 20.8%，所有纯阈值方案均未通过联合质量门。
- 新增 `DeterministicFAQScopePolicy`，只在 Embedding 前拒绝高置信天气、投资、医疗、代写、废止规则、内部规则和凭据索取。
- 范围门 v1 在开发集保持 Recall@5 100%，Decision 从 75% 升至 100%，FPR 从 100% 降至 0%。
- 冻结候选 `scope-gate-v1-threshold-0.10` 在 12 条 holdout 上一次验收：Recall 100%、Decision 100%、FPR 0%、Top-1 75%，质量门 PASS。
- LangGraph State 新增低基数范围/安全拒绝事件，控制台展示“业务范围门拒绝检索”；被拒问题不调用Embedding或Qdrant。
- 新增第 17 课、拒答实验卡、完整实施记录和候选契约测试；最终门禁为 Ruff PASS、Mypy 77 个源码文件 PASS、Pytest 175 passed/2 skipped、离线 Agent Eval 13/13、依赖锁 PASS。

## 2026-08-21：第 26 步 BM25 固定候选重排

- 新建 20 条排序开发集和 10 条从未运行的排序 holdout，覆盖退货、保修、发票和物流相邻政策竞争。
- 重排器只接收原向量 Top-5，融合归一向量分数与中文单字/双字 BM25，不允许新增候选文档。
- 开发集原序 Recall 100%、Top-1 80%、MRR 90%；25% BM25 后 Recall 100%、Top-1 95%、MRR 97.5%。
- 0.25 与 0.50 开发指标同分，按“同效果选择更小干预”冻结 `bm25-fusion-0.25`。
- 新排序 holdout 一次验收：原序 Top-1 90%/MRR 95%，冻结候选 Top-1 100%/MRR 100%，质量门 PASS。
- 开发与holdout候选集合违规均为0；结果来自重新排序，不是扩大召回池或放宽知识治理。
- 默认线上FAQ启用BM25 25%重排并公开有限事件；历史Agent/Qwen实验显式关闭重排，保持版本结果可复现。
- 新增第18课、重排面试卡和完整实施记录；最终门禁为 JavaScript语法 PASS、Ruff PASS、Mypy 79 个源码文件 PASS、Pytest 179 passed/2 skipped、离线 Agent Eval 13/13、依赖锁 PASS。

## 2026-08-21：第 27 步真实语义 Embedding 候选准备

- 新建16条语义开发集和10条未运行锁定集，重点覆盖低词面重合同义改写与近领域知识缺口。
- Hash最佳阈值0.10：Recall@5 75%、Top-1 41.7%、MRR 55.6%、决策准确率62.5%、负例误召回率75%，未通过质量门。
- 阈值提高到0.20后负例误召回率降至0，但Recall降至16.7%，证明词面Hash无法只靠阈值兼顾召回和拒答。
- 真实候选冻结为`qwen3.7-text-embedding`、1024维、单批20条；问题预先批量向量化并缓存，扫描六档阈值不重复收费。
- 23个知识切片分2批、16个开发问题分1批，首次开发实验计划3次业务层请求；服务商usage将记录实际Token和成本。
- 脚本默认完全离线；真实开发需要`--confirm-paid-api`，锁定集还要求开发阈值已冻结及`--confirm-holdout`。
- 真实开发候选按计划成功请求3次、消耗5,120输入Token、公开原价折算0.002560元；阈值扫描没有重复调用。
- 千问0.50是唯一通过联合门候选：Recall@5 91.7%、Top-1 83.3%、MRR 87.5%、决策准确率87.5%、FPR 25%。
- 已知失败为账号冒用问题被阈值拒绝，以及海外全球联保知识缺口误匹配保修文档；保留Bad Case，不改标签。
- 在运行语义holdout前冻结阈值0.50；锁定结果尚未产生，因此简历暂不写语义模型提升。
- 当前最终门禁为Ruff PASS、Mypy 80个源码文件 PASS、Pytest 180 passed/2 skipped。
- 语义holdout按冻结0.50一次运行：8条正例Recall/Top-1/MRR均100%，但2条负例中1条误召回，FPR 50%，质量门FAIL。
- 唯一锁定失败是“数字人民币和货到付款”误匹配“数字商品兑换规则”；属于主题相似但证据不蕴含答案。
- 候选不晋级，默认Embedding后端保持Hash；不能只展示正例100%而隐瞒安全失败。
- 已揭晓语义holdout转为下一版回归Bad Case，不再用于调阈值；下一步新建证据充分性开发/锁定实验。

## 2026-08-21：第 28 步证据充分性实验准备

- 将“问题+固定已召回Chunk”作为输入，只比较Extractive与千问Grounded回答器，隔离检索变量。
- 新建16条开发集（8条真正可回答、8条主题相似但无答案）和10条未运行的新holdout。
- 第27步“数字人民币/货到付款误召回数字商品规则”进入开发回归集，不再充当未知锁定样本。
- 指标包括有答案召回率、知识缺口正确拒答率、综合决策、无依据回答率和引用白名单合法率。
- Extractive离线基线：有答案召回100%、正确拒答0%、综合决策50%、无依据回答率100%、Gate FAIL。
- 真实候选复用线上`GroundedAnswerDraft.is_answerable`、Pydantic结构化输出和引用白名单，计划16次开发聊天调用。
- 系统提示SHA-256为`1c5a43de5b8f50dc4849911527fc233aa0b6aefa0197697b18382e2b48ccad4d`；开发通过后才冻结。
- 千问Grounded开发候选完成16次调用：有答案召回100%、正确拒答87.5%、综合决策93.75%、无依据回答率12.5%、引用合法100%，Gate PASS。
- 唯一失败为“是否免费上门取退货”错误放行；保留Bad Case，不为开发集100%临时修改提示。
- 已冻结通过开发门的系统提示SHA-256，之后只运行一次全新Grounding holdout，没有根据锁定结果回调提示。
- Grounding锁定对照：Extractive有答案召回100%、正确拒答0%、综合决策50%、无依据回答率100%、Gate FAIL。
- 千问Grounded锁定候选：有答案召回100%、正确拒答100%、综合决策100%、无依据回答率0%、引用合法100%、Gate PASS。
- 开发与锁定集合计26次真实聊天调用；锁定失败样本0条，`qwen-grounded-answer-v1`通过固定证据回答层晋级门。
- 该100%仅代表10条固定证据专项锁定题，不等于线上完整RAG幻觉为0；Embedding误召回与Chunk遗漏不在本实验变量内。
- 下一步新建端到端实验，把范围门、真实召回、候选重排、证据充分性和引用校验串联后再决定默认RAG晋级。
- 第28步结束时门禁为Ruff PASS、Mypy 81个源码文件 PASS、Pytest 182 passed/2 skipped、依赖锁PASS。

## 2026-08-21：第29步端到端RAG组合晋级门

- 新建16条开发题和12条未运行锁定题，从原始问题开始串联范围门、Embedding/Qdrant、BM25、Grounded判断和引用校验。
- 当前基线固定为deterministic_v1 + Hash 1024维/0.10 + BM25 25% + Extractive；候选固定为同范围门 + qwen3.7/0.50 + 同BM25 + qwen-plus Grounded。
- 候选SHA-256同时覆盖语料、切片、Embedding模型/维度、阈值、Top-K、BM25权重、Profile和Grounded系统提示。
- 离线基线：检索Recall 100%、Top-1 90.91%、有答案召回100%、正确拒答60%、综合决策87.5%、无依据回答率40%、Gate FAIL。
- 两个失败分别为支付方式误命中数字商品规则、免费上门取件误命中物流/运费/退货规则，说明近领域知识缺口不能只靠范围规则穷举。
- 总装首次暴露Extractive把Top-5长Chunk全部拼接后超过2000字符Schema上限；已改为按字符预算装入，并只引用真正写入答案的Chunk。
- 真实开发完成3次Embedding业务请求、5,040输入Token和13次聊天调用；候选检索Recall/Top-1/有答案召回均100%，正确拒答80%、综合决策93.75%、无依据回答率20%、引用合法100%，Gate PASS。
- 唯一失败仍是“普通退货能否免费安排快递上门取件”；它在第28步开发也失败，保留为Bad Case，不加特判刷100%。
- 候选在正确拒答与无依据回答两项上压线通过，完整指纹`197c37b7c7e888a685eb5e4d1a79b99b648da11f04311cdbe1a52bd61f649f47`已经冻结；随后只运行一次holdout且通过。
- 冻结候选一次运行12条端到端holdout：8条可回答、4条知识缺口；实际总计4次Embedding、5,340输入Token、22次聊天调用。
- 锁定基线检索Recall/Top-1均100%，但正确拒答50%、综合决策83.33%、无依据回答率50%，错误放行礼品卡有效期和周末指定配送两题，Gate FAIL。
- 千问候选锁定集检索Recall/Top-1/有答案召回/正确拒答/综合决策/引用合法均100%，无依据回答率0%，失败0条，Gate PASS。
- `qwen-e2e-rag-v1`晋级为已验证真实Profile；默认本地/CI仍保留Hash+Extractive以保证零费用和可重复，生产候选需显式开启。
- 锁定100%只对应12条小型新样本；开发集仍有上门取件Bad Case，不能表述为线上幻觉率0或永久准确率100%。
- 第29步离线最终门禁为Ruff PASS、Mypy 82个源码文件 PASS、Pytest 185 passed/2 skipped、依赖锁PASS。

## 2026-08-21：第30步GitHub发布验收

- 新增只读发布审计器：按`git ls-files --cached --others --exclude-standard`确定真实公开候选集合，不扫描被忽略的`.env`正文。
- 高置信扫描覆盖OpenAI兼容Key、私钥头、GitHub Token；配置赋值启发式只用于env/yaml/toml/ini，避免把Python变量和测试Token误报为秘密。
- `.env`、runtime、backups、`.idea`和虚拟环境均通过`git check-ignore -z`验证；Windows换行曾导致路径误判，改为空字符协议后修复。
- 新增可提交脱敏端到端摘要，保留指纹、调用量、聚合指标、失败Case ID和原报告SHA-256，不含问题、答案、向量、Key或用户数据。
- 首次审计283个公开候选文件：PASS 9、WARN 3、BLOCK 1；唯一Blocker为仓库尚无首次提交。
- 用户明确选择MIT并授权创建首次本地提交；版权主体暂写`ServiceOps Agent contributors`，不猜测真实姓名。
- 完成后审计284个公开候选文件：PASS 11、WARN 2、BLOCK 0、READY；Warning仅剩未配置origin和简历10处个人占位符。
- 最终门禁为Ruff PASS、Mypy 84个源码文件 PASS、Pytest 187 passed/2 skipped、依赖锁PASS。

## 2026-08-23：第31步独立Qdrant与完整混合召回实验

- 将纯向量、旧Top-5候选内BM25、全语料BM25和全语料Dense+BM25 RRF放进同变量四路对照。
- 已揭晓的4组历史数据合并为74条开发回归题；另建16条新holdout，参数冻结前默认不读取。
- 扫描RRF k 30/60和关键词权重0.40～2.00；开发冻结`hybrid-rrf-k60-lex1.50`。
- 开发纯向量Recall@5 100%、Top-1 83.87%、MRR 91.18%；冻结RRF Recall 100%、Top-1 93.55%、MRR 96.77%。
- RRF相对旧候选重排Top-1仅再提高1.61个百分点；逐题7条前移、1条后退。
- 当前小语料纯向量Top-5已全部召回，Dense+Lexical并集Recall仍100%、关键词救回0条；不能声称召回率提高。
- 冻结后一次运行16条新holdout：12条正例Recall 100%、Top-1 91.67%、MRR 95.83%；4条负例误召回2条，FPR 50%、Gate FAIL。
- 两条失败为“有雨吗”和“写一篇考试作文”未命中v1范围规则；不在同一holdout上补规则刷分。
- 开发优胜关键词权重1.50不晋级生产，现有默认0.80保持；下一步单独评测意图/范围分类漂移。

## 2026-08-23：第32步意图分类专项准备与离线基线

- 将全局路由四分类从13条整图评测中拆出，建立32条开发题和16条默认不读取的新holdout。
- 标签覆盖FAQ、订单查询、退货写请求和人工接管；第31步两个已揭晓范围Bad Case进入开发回归集。
- 新增Accuracy、Macro-F1、逐类Precision/Recall/F1、混淆矩阵、危险自动放行率和错误退货写路由率。
- 关键词基线：Accuracy 62.5%、Macro-F1 65.1%、Human Recall 60%、Unsafe Auto 40%、False Return 0%。
- FAQ分对3/8、订单7/8、退货4/6、人工6/10；规则精度型写路由安全性保留，但FAQ和域外表达覆盖不足。
- 千问候选固定结构化Intent Schema和当前提示SHA-256；计划32次开发调用，四档阈值复用同一批原始结果。
- 真实候选尚未运行，生产提示、0.65阈值和路由均未修改；等待用户显式`--confirm-paid-api`。
- 千问生产提示v1完成32次开发调用：四档阈值均为Accuracy 87.5%、Macro-F1 88.8%、Human Recall 80%、Unsafe Auto 20%、False Return 0%，Gate FAIL。
- 四条失败置信度均0.95：两条公开FAQ过度转人工，两条医疗/代码域外题危险放行为FAQ；无法靠阈值修复。
- 实验升级1.1.0：新增未晋级提示v2和“现有安全规则前置+千问v2”组合候选；两组复用同批32次结果，生产v1保持不变。
- 千问v2完成32次开发调用：全部四档阈值与两种装配均为Accuracy/Macro-F1/Human Recall 100%，Unsafe Auto/False Return 0%，Gate PASS。
- 原始置信度为15条0.95、15条0.98、2条0.99；四档同分不是低置信覆盖造成。
- 安全前置组合相对单独v2增益为0；按更少组件原则选择单独v2，同分阈值中冻结更保守的`qwen-intent-threshold-0.85`。
- 开发100%不写成最终效果；16条新holdout未运行，生产默认提示仍为v1。
- 冻结候选只调用16条新holdout，四类各4条；Accuracy/Macro-F1/Human Recall均100%，Unsafe Auto/False Return 0%，Gate PASS。
- v2总计48次真实调用（开发32+锁定16），晋级为生产真实模型提示；默认阈值从0.65更新为0.85。
- 默认mock/CI仍使用关键词规则，模型故障、低于0.85、Schema异常继续安全转人工。
- 100%只代表32条开发和16条一次性锁定题，不外推线上全量准确率。
- 生产提示、默认0.85阈值和实验提示已合并为同一来源，并用SHA-256指纹测试防止未评测改字；最终门禁为Ruff PASS、Mypy 87个源码文件 PASS、Pytest 202 passed/2 skipped、离线Agent Eval 13/13、Sphinx严格构建PASS、发布审计READY。
