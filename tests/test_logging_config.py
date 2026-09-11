"""单元测试：日志配置（级别解析、文件 handler、请求日志 request_id）"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask

from myapp.logging_config import RequestIdFilter, setup_logging

_LOG_ENV_VARS = ("LOG_LEVEL", "LOG_DIR", "LOG_FILE_BACKUP_DAYS", "LOG_SQL_LEVEL")
_MANAGED_LOGGERS = ("myapp", "myapp.request", "sqlalchemy", "werkzeug")


class _ListHandler(logging.Handler):
    """把日志记录收进列表，便于断言"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:  # type: ignore[reportUnusedFunction]
    """清空日志相关环境变量，并在测试结束后还原 dictConfig 改写的全局 logger 状态"""
    for name in _LOG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved: dict[str, tuple[int, list[logging.Handler], bool]] = {}
    for name in _MANAGED_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = (logger.level, list(logger.handlers), logger.propagate)

    yield

    root.setLevel(saved_root[0])
    root.handlers = saved_root[1]
    for name, (level, handlers, propagate) in saved.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler) and handler not in handlers:
                handler.close()
        logger.setLevel(level)
        logger.handlers = handlers
        logger.propagate = propagate


class TestLevelConfig:
    def test_default_level_is_info(self) -> None:
        setup_logging(Flask(__name__))

        assert logging.getLogger("myapp").level == logging.INFO
        assert logging.getLogger().level == logging.INFO

    def test_env_level_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "debug")

        setup_logging(Flask(__name__))

        assert logging.getLogger("myapp").level == logging.DEBUG

    def test_invalid_level_falls_back_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_LEVEL", "verbose")

        setup_logging(Flask(__name__))

        assert logging.getLogger("myapp").level == logging.INFO

    def test_sqlalchemy_level_is_lowered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOG_SQL_LEVEL", "INFO")

        setup_logging(Flask(__name__))

        assert logging.getLogger("sqlalchemy").level == logging.INFO


class TestHandlers:
    def test_console_handler_only_by_default(self) -> None:
        setup_logging(Flask(__name__))

        handlers = logging.getLogger("myapp").handlers
        assert any(isinstance(handler, logging.StreamHandler) for handler in handlers)
        assert not any(isinstance(handler, logging.FileHandler) for handler in handlers)

    def test_log_dir_adds_rotating_file_handler(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("LOG_DIR", str(tmp_path))

        setup_logging(Flask(__name__))

        file_handlers = [h for h in logging.getLogger("myapp").handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1

        logging.getLogger("myapp.demo").warning("hello-logging")
        for handler in file_handlers:
            handler.flush()

        content = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert "hello-logging" in content
        assert "myapp.demo" in content


class TestRequestLogging:
    def test_request_id_is_echoed_and_logged(self, app: Flask) -> None:
        captured = _ListHandler()
        captured.addFilter(RequestIdFilter())
        access_logger = logging.getLogger("myapp.request")
        original_level = access_logger.level
        access_logger.addHandler(captured)
        access_logger.setLevel(logging.INFO)

        try:
            response = app.test_client().get("/__not_found__", headers={"X-Request-ID": "trace-abc"})
        finally:
            access_logger.removeHandler(captured)
            access_logger.setLevel(original_level)

        assert response.headers["X-Request-ID"] == "trace-abc"
        assert any(getattr(record, "request_id", None) == "trace-abc" for record in captured.records)

    def test_request_id_is_generated_when_absent(self, app: Flask) -> None:
        response = app.test_client().get("/__not_found__")

        assert response.headers["X-Request-ID"] != "-"
