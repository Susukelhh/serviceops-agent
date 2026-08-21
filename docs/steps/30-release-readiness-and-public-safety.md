# 第30步：GitHub发布验收与公开安全

## 这一步解决什么问题

代码能运行，不等于项目适合公开。公开前至少要回答四个问题：

1. `git add`会不会把`.env`、数据库、备份或IDE文件一起带走？
2. 简历里的实验数字能否在GitHub找到可复核、又不泄漏问题正文的证据？
3. README链接、Ruff、Mypy、Pytest和依赖锁是否仍然有效？
4. 哪些动作必须由项目所有者本人决定，例如首次提交、远程仓库和许可证？

第30步脚本只读检查Git和项目文件，只写一份被`.gitignore`排除的本地报告。它不会执行`git add`、
`git commit`、`git push`，不会创建GitHub仓库，也不会自动选择许可证。

## 自动检查内容

| 检查 | PASS含义 | BLOCK含义 |
|---|---|---|
| Git公开候选集合 | 使用与Git相同的ignore规则确定未来提交文件 | 当前不是Git仓库或集合异常 |
| 高置信秘密扫描 | 未发现`sk-...`、私钥头、GitHub Token和非占位敏感赋值 | 存在可疑文件/行号；报告不打印秘密值 |
| 私有路径忽略 | `.env`、runtime、backups、IDE、虚拟环境都被忽略 | 普通`git add`可能带入本地数据 |
| README相对链接 | GitHub入口链接都能解析到项目文件 | 面试官会遇到404 |
| 冻结RAG证据 | 可提交摘要与冻结指纹、12条锁定规模和Gate一致 | 简历数字可能引用错误版本 |
| Git首次提交 | 存在可恢复的HEAD版本快照 | 所有项目文件仍未进入历史 |
| Ruff/Mypy/Pytest/Lock | 显式开启后全部本地质量门通过 | 发布声明与当前代码不一致 |

秘密扫描只能降低常见误提交风险，不能证明绝对没有秘密。第一次提交前仍要本人查看`git diff --cached`，
如果密钥曾经进入提交历史，仅从工作区删除还不够，还需要轮换密钥并清理历史。

## 脱敏实验摘要

原始真实实验报告保存在`data/runtime`并受`.gitignore`保护。GitHub证据改为：

```text
data/evaluation/results/rag_end_to_end_v1_frozen_result.json
```

它保留候选指纹、模型名、调用计数、聚合指标、失败Case ID和原报告SHA-256，但删除问题正文、答案正文、
隐藏推理、向量、Key和用户数据。摘要同时写明12条小型锁定集不能外推线上准确率。

## PyCharm运行配置

- Name：`30 Release readiness audit`
- Script path：`D:\serviceops-agent\examples\30_release_readiness_audit.py`
- Parameters：`--run-quality-gates`
- Working directory：`D:\serviceops-agent`
- Interpreter：`D:\serviceops-agent\.venv\Scripts\python.exe`
- Environment variables：`PYTHONUTF8=1`

首次发布审计曾因仓库没有提交而返回退出码1和`NOT READY`。用户随后明确选择MIT许可证并授权创建首次本地
提交；再次审计只有`BLOCK=0`才输出`READY`。远程仓库仍未配置，因此没有发生任何网络推送。

## 当前实测结果

使用`--run-quality-gates`完成一次本地验收：

```text
Git公开候选文件：284
PASS：11
WARN：2
BLOCK：0
Ruff：PASS
Mypy：84个源码文件PASS
Pytest：187 passed，2 skipped
依赖锁：PASS
公开推送结论：READY
```

MIT许可证和首次本地提交已经完成。两个Warning分别是未配置`origin`、简历还有10处个人信息占位符。
许可证版权主体暂用`ServiceOps Agent contributors`，避免猜测用户真实姓名；远程仓库和个人信息仍由用户处理。

## 当前不能自动完成的事项

- 填写真实姓名、学校、毕业日期、电话、邮箱和GitHub链接；
- 在GitHub创建仓库、设置公开/私有属性和添加远程地址；
- 本人审查远程地址后执行首次推送；
- 确认自己能够解释简历保留的每一条项目内容。

## 面试表达

> 项目公开前我没有直接把整个目录推到GitHub，而是按Git实际候选文件做秘密扫描，验证`.env`、运行数据库、
> 备份和IDE目录不会进入提交；真实模型原始报告留在runtime，只提交与冻结指纹一致的脱敏聚合摘要。发布
> 脚本还检查README断链、Git历史和可选质量门，但不会自动提交或替作者选择许可证。

## 权威资料

- [GitHub：Removing sensitive data from a repository](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)
- [GitHub：Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)
- [Git：gitignore文档](https://git-scm.com/docs/gitignore)
