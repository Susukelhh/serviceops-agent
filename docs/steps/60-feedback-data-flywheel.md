# 第60步：用户反馈、失败问题池与知识候选飞轮

## 完成结果

本步把“答不上来以后怎么办”从一句产品口号变成了持久化业务闭环：

```text
用户点赞/点踩 ─┐
自动转人工信号 ─┴→ 反馈问题池 → 知识审核员归因
                                  ├→ 检索/生成/工作流问题
                                  ├→ 无效反馈关闭
                                  └→ 知识缺口候选 → 离线评测 → 版本化发布
```

## 安全与一致性边界

- 普通用户只能反馈自己的会话轮次，不存在和越权统一返回404；
- 反馈使用独立幂等键，相同请求重放不创建重复问题；
- 正向反馈不能夹带失败原因，负向反馈必须使用有限原因码；
- `knowledge_curator`只有`feedback:review`权限，不能对话、审批、读审计或做运维；
- 同一反馈只能接受一个审核决定；相同决定可以幂等重放，不同决定返回冲突；
- 知识缺口必须给出标题和候选答案，其他归因不能夹带待发布正文；
- 会话隐私删除会同时删除反馈和候选内容；
- 审核成功不会直接修改活动Qdrant，避免未经评测的答案立即污染线上知识。

## API

```text
POST /api/v1/conversations/{conversation_id}/turns/{turn_id}/feedback
GET  /api/v1/internal/feedback
POST /api/v1/internal/feedback/{feedback_id}/review
GET  /api/v1/internal/feedback/knowledge-candidates
```

非审批型人工接管会自动以`auto_handoff`进入问题池；它是改进旁路，问题池暂时不可用不会改变已经安全完成的用户响应。

## 候选发布

SQLite或PostgreSQL环境可以运行：

```powershell
uv run python examples/60_export_feedback_knowledge_candidates.py
```

输出位于`data/runtime/feedback_knowledge_candidates.json`。其中知识文档保持`draft`状态，后续必须先进入现有端到端RAG评测和RAGAS兼容评测；只有通过晋级门后，才允许合并进版本化知识源并重建索引。

## 代码与测试

- 领域契约：`src/serviceops_agent/domain/feedback.py`
- 三种仓库：`src/serviceops_agent/infrastructure/feedback_repository.py`
- PostgreSQL迁移：`src/serviceops_agent/migrations/versions/20260902_0004_feedback_flywheel.py`
- API测试：`tests/api/test_feedback_api.py`
- 仓库契约测试：`tests/unit/test_feedback_repository.py`
