import logging
import os
import pathlib
from logging.handlers import RotatingFileHandler


class ExactLevelFilter(logging.Filter):
    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


class MinLevelFilter(logging.Filter):
    def __init__(self, min_level: int):
        super().__init__()
        self.min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.min_level


format_str = (
    "[%(levelname)s] [%(name)s] %(asctime)s %(filename)s %(lineno)s %(message)s"
)
formatter = logging.Formatter(format_str)


class TaskLogger(logging.LoggerAdapter):
    """任务级日志适配器：既支持 ``log("msg")`` 单参数调用（兼容旧接口），

    又提供标准 ``.info/.warning/.error/.debug`` 方法。

    自动在消息前拼接 ``[#task]`` 前缀（task 来自 extra）；
    account 字段不自动拼接——多账号共享同一 engine 时由调用方动态注入，
    避免与内部 ``[{account}]`` 重复。
    """

    def process(self, msg, kwargs):
        extra = self.extra or {}
        task = extra.get("task")
        if task:
            msg = f"[#{task}] {msg}"
        return msg, kwargs

    def __call__(self, msg: str) -> None:  # 兼容 giveaway_core 等的 Callable 注入
        self.info(msg)


def make_task_logger(
    workdir: str | pathlib.Path,
    task_name: str,
    account: str = "",
    *,
    log_file: str = "task.log",
    log_level: str = "INFO",
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 10,
) -> TaskLogger:
    """工厂：按任务/账号生成带独立落盘文件的 logger。

    返回对象同时满足：
    - callable: ``log("msg")`` → info 级别输出到 stdout + 落盘
    - 标准 logger 方法: ``log.warning("...")``, ``log.error("...")``

    文件名形如 ``<workdir>/logs/giveaway.log``，自动按大小滚动。
    """
    log_dir = pathlib.Path(workdir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_file

    # 按 workdir / task / account 构造唯一 logger 名，避免多账号互相污染 handler
    logger_name = f"tg-signer.task.{task_name}.{account}" if account else f"tg-signer.task.{task_name}"
    level = log_level.strip().upper()
    level_no: int = logging.getLevelName(level)

    logger = logging.getLogger(logger_name)
    logger.setLevel(level_no)
    logger.propagate = False  # 不上浮到 root，避免重复输出

    # 避免重复添加 handler（同一进程多次调用时）
    if not logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    extra = {"task": task_name, "account": account}
    return TaskLogger(logger, extra)


def configure_logger(
    name: str = "tg-signer",
    log_level: str = "INFO",
    log_dir: str | pathlib.Path = "logs",
    log_file: str | pathlib.Path = None,
    max_bytes: int = 1024 * 1024 * 3,
):
    level = log_level.strip().upper()
    level_no: int = logging.getLevelName(level)
    logger = logging.getLogger(name)
    logger.setLevel(level_no)
    logger.handlers.clear()
    logger.propagate = False

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_dir = pathlib.Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_file or log_dir / f"{name}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if logging.WARNING >= level_no:
        warn_file_handler = RotatingFileHandler(
            log_dir / "warn.log",
            maxBytes=max_bytes,
            backupCount=10,
            encoding="utf-8",
        )
        warn_file_handler.setLevel(logging.WARNING)
        warn_file_handler.addFilter(ExactLevelFilter(logging.WARNING))
        warn_file_handler.setFormatter(formatter)
        logger.addHandler(warn_file_handler)

    if logging.ERROR >= level_no:
        error_file_handler = RotatingFileHandler(
            log_dir / "error.log",
            maxBytes=max_bytes,
            backupCount=10,
            encoding="utf-8",
        )
        error_file_handler.setLevel(logging.ERROR)
        error_file_handler.addFilter(MinLevelFilter(logging.ERROR))
        error_file_handler.setFormatter(formatter)

        logger.addHandler(error_file_handler)
    if os.environ.get("PYROGRAM_LOG_ON", "0") == "1":
        pyrogram_logger = logging.getLogger("pyrogram")
        pyrogram_logger.setLevel(level)
        pyrogram_logger.addHandler(console_handler)
    return logger
