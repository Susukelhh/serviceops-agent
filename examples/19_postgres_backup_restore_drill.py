"""第十九步示例：创建 PostgreSQL 备份并完成一次隔离恢复演练。

运行前先启动 Docker Compose，然后直接在 PyCharm 运行本文件，或执行：

    uv run python examples/19_postgres_backup_restore_drill.py

脚本不会停止当前 API，也不会删除或覆盖真实 ``serviceops`` 数据库。它会生成一个 custom 格式
备份，恢复到随机临时数据库，逐表比较行数和内容指纹，最后删除临时数据库并保留已验证备份。
备份可能包含业务与对话状态，``data/backups`` 不得提交 Git 或发送给无权限人员。
"""

# PostgresBackupError 让命令行只打印有限失败原因，不向学习者倾倒完整堆栈。
from serviceops_agent.operations.postgres_backup import (
    PostgresBackupError,
    run_backup_restore_drill,
)


def main() -> int:
    """运行第十九步并以退出码表达成功或失败。"""

    # 开始提示明确本次动作会保留备份，但恢复目标只是临时数据库。
    print("=== ServiceOps Agent 第19步：PostgreSQL 备份与隔离恢复演练 ===")
    # 提醒操作者不要把包含数据的归档当作普通源码文件。
    print("说明：不会修改真实业务库；备份文件属于敏感运行数据，请妥善保管。")
    try:
        # 可复用运维组件完成健康检查、备份、恢复、比较和清理。
        artifact = run_backup_restore_drill()
    except PostgresBackupError as error:
        # 错误消息由组件进行长度限制，并且命令参数不包含数据库密码。
        print(f"FAIL 第19步：{error}")
        # 非零退出码方便 PyCharm、PowerShell 和 CI 识别失败。
        return 1

    # PASS 只会在临时恢复库内容一致且已经成功删除后输出。
    print("PASS 第19步：备份可读取、可恢复、逐表一致，临时数据库已清理")
    # 文件大小帮助快速识别明显异常的空归档。
    print(f"备份大小：{artifact.size_bytes} bytes")
    # 表数量包含业务、Alembic 和 LangGraph Checkpoint 表。
    print(f"验证表数：{artifact.table_count}")
    # 完整 SHA-256 可以和 manifest 或异地副本做一致性核对。
    print(f"SHA-256：{artifact.sha256}")
    # 使用绝对路径，PyCharm 终端通常可以直接点击打开。
    print(f"备份文件：{artifact.backup_path}")
    # 清单保存工具版本、表级指纹和恢复验证结论。
    print(f"备份清单：{artifact.manifest_path}")
    # 固定报告供后续发布门禁或简历演示读取。
    print(f"运行报告：{artifact.report_path}")
    # 返回零表示整条演练链路通过。
    return 0


# 只有直接运行文件时才执行演练；pytest 导入模块不会访问 Docker。
if __name__ == "__main__":
    # SystemExit 把 main 的返回值传给 PyCharm 和终端。
    raise SystemExit(main())
