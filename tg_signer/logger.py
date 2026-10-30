import logging
import os
import pathlib
import threading
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


# Pyrogram 传输层写 ``Send failed: ...`` 的 logger（连接被重置时会在
# 数秒内对每条在途请求各打一行，需要按秒聚合）
TRANSPORT_LOGGER_NAME = "pyrogram.connection.transport.tcp.tcp"


class SecondWindowAggregator(logging.Filter):
    """把同一前缀的日志按秒聚合：窗口内首次命中先扣住，窗口结束时一次输出。

    命中多次时输出一行并在末尾追加重复次数；只命中一次则原样延后输出。
    未命中前缀的记录立即放行，不影响其他日志。
    """

    def __init__(self, window: float = 1.0, prefixes=("Send failed",)):
        super().__init__()
        self.window = window
        self.prefixes = prefixes
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._timer: threading.Timer | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_aggregate_flushed", False):
            return True
        msg = record.getMessage()
        if not any(msg.startswith(prefix) for prefix in self.prefixes):
            return True
        with self._lock:
            item = self._pending.get(msg)
            if item is None:
                item = [0, record]
                self._pending[msg] = item
                if self._timer is None:
                    self._timer = threading.Timer(self.window, self._flush)
                    self._timer.daemon = True
                    self._timer.start()
            item[0] += 1
        return False

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending, self._timer = self._pending, {}, None
        for count, record in pending.values():
            if count == 1:
                out = record
            else:
                out = logging.LogRecord(
                    record.name,
                    record.levelno,
                    record.pathname,
                    record.lineno,
                    f"{record.getMessage()}（{self.window:g} 秒内重复 {count} 次）",
                    None,
                    record.exc_info,
                    record.funcName,
                )
            out._aggregate_flushed = True
            logging.getLogger(record.name).handle(out)


def install_transport_log_aggregator(window: float = 1.0) -> None:
    """给 Pyrogram 传输层日志安装按秒聚合过滤器（同一 logger 重复调用幂等）。"""
    target = logging.getLogger(TRANSPORT_LOGGER_NAME)
    if any(isinstance(f, SecondWindowAggregator) for f in target.filters):
        return
    target.addFilter(SecondWindowAggregator(window))


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
    install_transport_log_aggregator()

    # 按 workdir / task / account 构造唯一 logger 名，避免多账号互相污染 handler
    logger_name = (
        f"tg-signer.task.{task_name}.{account}"
        if account
        else f"tg-signer.task.{task_name}"
    )
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
    install_transport_log_aggregator()

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
