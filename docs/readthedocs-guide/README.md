# ServiceOps Agent 教学文档

这是一个标准 Sphinx 源文档目录，主题使用 `sphinx-rtd-theme`。

在 `D:\serviceops-agent` 中重新构建：

```powershell
D:\Python\Scripts\uv.exe run --with sphinx --with sphinx-rtd-theme sphinx-build -W --keep-going -b html docs/readthedocs-guide docs/readthedocs-guide/_build/html
```

构建成功后打开：

```text
D:\serviceops-agent\docs\readthedocs-guide\_build\html\index.html
```

目录说明：

- `index.rst`：首页和左侧目录树；
- `modules/`：项目各技术模块；
- `interview/`：HR、技术面和压力追问题库；
- `_static/custom.css`：固定左侧目录及中文阅读样式；
- `_build/`：本地生成页面，已被 Git 忽略。
