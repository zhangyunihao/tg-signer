"""tg-signer 命令行入口，仅保留启动 WebUI 的命令。

其余所有功能（登录、签到、自动化、监控、消息发送等）一律通过 WebUI 操作，
内部任务子进程由 `tg_signer.task_runner` 承接，不再走命令行。
"""

import argparse
import os


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-signer-plus",
        description="tg-signer 命令行入口（仅保留启动命令，其余操作请在网页界面完成）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gui = sub.add_parser("webgui", help="启动 WebUI")
    gui.add_argument("--host", "-H", default="127.0.0.1", help="监听地址")
    gui.add_argument("--port", "-P", type=int, default=8080, help="监听端口")
    gui.add_argument(
        "--storage-secret",
        "-S",
        default=None,
        help="存储密钥，若不输入则使用已持久化的密钥或每次启动随机生成",
    )
    gui.add_argument(
        "--auth-code",
        default=None,
        help="授权码，也可通过环境变量`TG_SIGNER_GUI_AUTHCODE`设置。若存在则访问界面时需要正确输入。",
    )
    return parser


def tg_signer(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from tg_signer.webui import AUTH_CODE_ENV, main

    if args.auth_code:
        os.environ[AUTH_CODE_ENV] = args.auth_code
    main(host=args.host, port=args.port, storage_secret=args.storage_secret)
    return 0
