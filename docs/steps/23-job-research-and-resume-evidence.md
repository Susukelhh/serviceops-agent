# 第 23 步：把 Agent 项目变成可投递的简历证据

## 本步解决的问题

项目做得复杂不等于招聘方能在十几秒内看懂。本步先调研 2026 年 8 月杭州的应届、实习和资深 Agent
岗位，再把岗位高频要求映射到项目中的真实代码、测试和实验报告，形成一页式中文简历内容初稿。

## 新增交付

- `career/research/hangzhou-agent-job-requirements-2026-08.md`：杭州岗位样本、频次和投递优先级；
- `career/resume/serviceops-agent-resume-draft.md`：带个人信息占位符的一页式中文简历初稿；
- `career/resume/project-evidence-map.md`：简历主张、代码位置、追问和回答主线；
- `career/resume/personal-info-checklist.md`：生成最终简历前必须补齐的真实资料；
- `tests/unit/test_career_materials.py`：防止未来把未实现技术、虚假生产经历或失去范围的数据写入材料。

## 为什么暂时不直接生成最终 PDF

仓库没有姓名、学校、专业、联系方式、毕业日期和真实其他经历。直接生成“看起来完整”的 PDF 必然要么
残留占位符，要么编造个人信息。当前先完成内容和证据，等信息补齐后再针对具体 JD 排版成一页 PDF。

## 数据表达边界

- `13/13` 是版本 1.0.0 的 13 条小型 Agent 黄金回归集，不是线上全量准确率；
- 千问候选实验是 `qwen-plus`、实验契约 1.1.0、连续三轮，每轮 13/13；
- 第 22 步结束时为 `162 passed、2 skipped`；加入三条求职材料契约测试后，当前完整门禁为
  `165 passed、2 skipped`；
- Docker 限流、双实例和恢复都是本机实验，不冒充云生产服务；
- 项目使用 AI 辅助开发，求职者必须完成证据地图中的所有权验收后再保留相应表述。

## 下一步

填写个人信息补充表，提供第一批 3–5 个真实目标 JD，然后生成：

1. Agent 应用开发主版本；
2. Python AI 后端侧重版本；
3. 每个 JD 的关键词微调清单；
4. 正式一页 DOCX/PDF；
5. 与简历每条项目要点对应的面试问答。

## 本步验证结果

- 求职材料专项：3 passed；
- Ruff：PASS；
- Mypy：74 个源码文件 PASS；
- Pytest：165 passed、2 skipped；
- 离线 Agent Eval：13/13，四维指标 100%，质量门 PASS；
- `uv.lock`：PASS。
