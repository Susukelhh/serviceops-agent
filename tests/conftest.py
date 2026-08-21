"""pytest 全局测试配置。

无论开发者本地 `.env` 是否启用了真实模型，自动测试都强制使用 mock 分类基线，避免测试
产生外部网络请求、费用和不可重复结果。
"""

# os 用于在应用模块导入前设置仅对当前 pytest 进程有效的环境变量。
import os

# 强制覆盖模型后端；子测试进程结束后不会修改用户磁盘上的 `.env` 文件。
os.environ["SERVICEOPS_LLM_BACKEND"] = "mock"
# 强制使用本地 Hash Embedding，防止用户 `.env` 启用千问向量后测试产生网络费用。
os.environ["SERVICEOPS_EMBEDDING_BACKEND"] = "hash"
# 强制使用确定性摘录回答器，生成安全测试不会调用真实聊天模型。
os.environ["SERVICEOPS_RAG_GENERATION_BACKEND"] = "extractive"
# 测试索引只保存在当前进程内存中，不污染开发者的持久化 Qdrant 目录。
os.environ["SERVICEOPS_QDRANT_LOCATION"] = ":memory:"
# API 回归测试继续使用进程内 Checkpointer，避免写入开发者的 data/runtime 目录。
os.environ["SERVICEOPS_PERSISTENCE_BACKEND"] = "memory"
# 自动测试关闭后台遥测导出线程，避免控制台噪声和进程级 Provider 污染测试隔离。
os.environ["SERVICEOPS_TELEMETRY_ENABLED"] = "false"
# 测试使用独立且足够长的固定签名密钥，不依赖开发者 `.env`，Token 结果可重复验证。
os.environ["SERVICEOPS_JWT_SECRET_KEY"] = "serviceops-test-only-jwt-secret-32-characters-minimum"
