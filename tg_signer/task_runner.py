"""内部任务子进程入口（仅供 WebUI 拉起，非用户命令）。

用法（每账号一个进程，由 WebUI 或 combo_runner 调用）::

    python -m tg_signer.task_runner -w <workdir> -a <account> run-once <task>
    python -m tg_signer.task_runner -w <workdir> -a <account> run <task> [task...]
    python -m tg_signer.task_runner -w <workdir> -a <account> automation run <task>
    python -m tg_signer.task_runner -w <workdir> -a <account> monitor run <task>

执行逻辑与原 CLI 对应命令完全一致：日志写入 stdout 由 WebUI 解析，
任一异常以非 0 退出码结束。
"""

import asyncio
import logging
import os
import sys

from tg_signer.core import ChatFolderError

logger = logging.getLogger(__name__)


def _parse(argv: list[str]) -> tuple[str, str, list[str]]:
    """解析 -w/-a 全局选项，返回 (workdir, account, 剩余动作参数)。"""
    workdir = ".signer"
    account = "my_account"
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-w", "--workdir") and i + 1 < len(argv):
            workdir = argv[i + 1]
            i += 2
        elif arg in ("-a", "--account") and i + 1 < len(argv):
            account = argv[i + 1]
            i += 2
        else:
            break
    rest = argv[i:]
    # "automation run" / "monitor run" 两段式动作归一为单段
    if (
        rest
        and rest[0] in ("automation", "monitor")
        and len(rest) >= 2
        and rest[1] == "run"
    ):
        rest = [f"{rest[0]}-run", *rest[2:]]
    return workdir, account, rest


def _worker_kwargs(workdir: str, account: str) -> dict:
    from tg_signer.core import get_proxy

    return {
        "account": account,
        "proxy": get_proxy(os.environ.get("TG_PROXY")),
        "session_dir": ".",
        "workdir": workdir,
        "session_string": os.environ.get("TG_SESSION_STRING"),
        "in_memory": False,
    }


def main(argv: list[str] | None = None) -> int:
    from tg_signer.logger import configure_logger

    # Windows 控制台默认 GBK，打印含 emoji/特殊符号的日志会直接崩溃，改为 UTF-8 并容错
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            logger.debug("重配置控制台输出流编码为 UTF-8 失败", exc_info=True)

    workdir, account, rest = _parse(list(sys.argv[1:] if argv is None else argv))
    if not rest:
        print(
            "缺少任务动作参数（run-once / run / automation run / monitor run）",
            file=sys.stderr,
            flush=True,
        )
        return 2

    root_logger = configure_logger(
        log_level="info", log_dir="logs", log_file="logs/tg-signer.log"
    )
    proxy = os.environ.get("TG_PROXY")
    if proxy:
        root_logger.info("Using proxy: %s" % proxy)
    root_logger.info(f"Using account: {account}")

    action = rest[0]
    task_names = rest[1:]
    if not task_names:
        print("缺少任务名称参数", file=sys.stderr, flush=True)
        return 2

    kwargs = _worker_kwargs(workdir, account)

    try:
        if action == "run-once":
            from tg_signer.core import UserSigner

            signer = UserSigner(task_name=task_names[0], **kwargs)
            signer.app_run(signer.run_once(200))
        elif action == "run":
            from tg_signer.core import UserSigner

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            coros = []
            for task_name in task_names:
                signer = UserSigner(task_name=task_name, loop=loop, **kwargs)
                coros.append(signer.run(200))
            try:
                loop.run_until_complete(asyncio.gather(*coros))
            finally:
                loop.close()
        elif action == "automation-run":
            from tg_signer.automation import UserAutomation

            automation = UserAutomation(task_name=task_names[0], **kwargs)
            automation.app_run(automation.run())
        elif action == "monitor-run":
            from tg_signer.core import UserMonitor

            monitor = UserMonitor(task_name=task_names[0], **kwargs)
            monitor.app_run(monitor.run())
        else:
            print(f"未知任务动作: {action}", file=sys.stderr, flush=True)
            return 2
    except ChatFolderError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
