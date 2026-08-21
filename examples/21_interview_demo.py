"""第21步示例：一键准备 ServiceOps Agent 面试演示环境。

在 PyCharm 中直接运行本文件即可完成默认流程：重新构建并启动 Docker、等待健康、执行真实冒烟、
打印短期角色 Token，并打开本地 Agent 控制台。默认使用 Compose 中的 mock/deterministic 配置，
不会调用千问，也不会自动批准退货写操作。
"""

# argparse 提供三个明确关闭开关；不传参数就是最稳妥的完整面试预检。
import argparse

# prepare_interview_demo 是经过单元测试的核心流程；示例文件只负责命令行参数。
from serviceops_agent.demo.interview_demo import DemoPreflightError, prepare_interview_demo


def _parse_arguments() -> argparse.Namespace:
    """读取是否跳过构建、浏览器或 Token 输出。"""

    # parser 的说明会出现在 PyCharm Parameters 配错时的 --help 输出中。
    parser = argparse.ArgumentParser(
        description="一键准备 ServiceOps Agent 本地面试演示",
    )
    # --no-build 适合刚刚构建过且代码未变化的快速复检。
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="复用现有镜像，不重新构建",
    )
    # --no-browser 适合远程终端或只想检查环境的情况。
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="检查完成后不自动打开本地控制台",
    )
    # --no-tokens 适合只验证 Docker；网页实际演示仍需另行运行第09步生成身份。
    parser.add_argument(
        "--no-tokens",
        action="store_true",
        help="不在当前终端输出短期角色 Token",
    )
    # parse_args 会自动拒绝未知参数，避免拼写错误悄悄改变演示行为。
    return parser.parse_args()


def main() -> int:
    """把 PyCharm 参数转换成启动器布尔选项，并返回标准退出码。"""

    # 只解析本示例的三个关闭开关，不读取用户问题或业务标识。
    arguments = _parse_arguments()
    try:
        # no-* 参数需要取反，得到核心函数使用的正向语义。
        prepare_interview_demo(
            build_image=not arguments.no_build,
            open_browser=not arguments.no_browser,
            print_tokens=not arguments.no_tokens,
        )
    except DemoPreflightError as error:
        # 只输出启动器生成的有限步骤错误，不打印环境变量、命令参数或 Token。
        print(f"\nFAIL 第21步：{error}")
        return 1
    # 所有步骤通过才返回 0，PyCharm 会显示“进程已结束，退出代码为 0”。
    return 0


# 被测试或文档导入时不启动 Docker；只有直接运行本文件才执行。
if __name__ == "__main__":
    # SystemExit 把 main 的 0/1 状态传给 PyCharm 和自动化系统。
    raise SystemExit(main())
