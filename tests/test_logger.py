import logging
import time
from typing import List

from tg_signer.logger import (
    TRANSPORT_LOGGER_NAME,
    SecondWindowAggregator,
    install_transport_log_aggregator,
)

TEST_LOGGER_NAME = "tg-signer.test.transport"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _wait_for(handler: _Capture, count: int = 1, timeout: float = 2.0) -> None:
    # 聚合由后台计时器触发，测试里轮询等待结果，避免依赖固定 sleep
    deadline = time.time() + timeout
    while time.time() < deadline and len(handler.records) < count:
        time.sleep(0.01)


def _emit_failed(logger: logging.Logger, times: int = 1) -> None:
    for _ in range(times):
        logger.error("Send failed: %s %s", "ConnectionResetError", "Connection lost")


def test_same_message_within_window_is_aggregated() -> None:
    logger = logging.getLogger(TEST_LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    aggregator = SecondWindowAggregator(window=0.05)
    handler = _Capture()
    logger.addFilter(aggregator)
    logger.addHandler(handler)
    try:
        _emit_failed(logger, times=3)
        _wait_for(handler, 1)
        assert len(handler.records) == 1
        assert "重复 3 次" in handler.records[0].getMessage()
        assert handler.records[0].levelno == logging.ERROR
    finally:
        logger.removeFilter(aggregator)
        logger.removeHandler(handler)


def test_single_message_is_emitted_once_after_window() -> None:
    logger = logging.getLogger(TEST_LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    aggregator = SecondWindowAggregator(window=0.05)
    handler = _Capture()
    logger.addFilter(aggregator)
    logger.addHandler(handler)
    try:
        _emit_failed(logger)
        assert handler.records == []
        _wait_for(handler, 1)
        assert len(handler.records) == 1
        assert handler.records[0].getMessage().startswith("Send failed")
        assert "重复" not in handler.records[0].getMessage()
    finally:
        logger.removeFilter(aggregator)
        logger.removeHandler(handler)


def test_other_transport_messages_pass_through_immediately() -> None:
    logger = logging.getLogger(TEST_LOGGER_NAME)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    aggregator = SecondWindowAggregator(window=0.05)
    handler = _Capture()
    logger.addFilter(aggregator)
    logger.addHandler(handler)
    try:
        logger.error("Unable to connect due to network issues: Connection timed out")
        assert len(handler.records) == 1
        _wait_for(handler, 2, timeout=0.2)
        assert len(handler.records) == 1
    finally:
        logger.removeFilter(aggregator)
        logger.removeHandler(handler)


def test_install_is_idempotent() -> None:
    logger = logging.getLogger(TRANSPORT_LOGGER_NAME)
    for existing in [
        f for f in logger.filters if isinstance(f, SecondWindowAggregator)
    ]:
        logger.removeFilter(existing)
    try:
        install_transport_log_aggregator()
        install_transport_log_aggregator()
        installed = [f for f in logger.filters if isinstance(f, SecondWindowAggregator)]
        assert len(installed) == 1
    finally:
        for existing in [
            f for f in logger.filters if isinstance(f, SecondWindowAggregator)
        ]:
            logger.removeFilter(existing)
