"""集中式日志配置（标准库 ``logging`` + ``dictConfig``，无第三方依赖）。

用法：在应用工厂中尽早调用 ``setup_logging(app)``，保证启动早期（如读取数据库配置之前）
的日志也能被正常输出。

环境变量：

- ``LOG_LEVEL``：全局日志级别，默认 ``INFO``
- ``LOG_DIR``：设置后额外写入按天轮转的日志文件（目录不存在会自动创建）；留空则仅输出到 stdout
- ``LOG_FILE_BACKUP_DAYS``：日志文件保留天数，默认 ``7``（仅在设置 ``LOG_DIR`` 后生效）
- ``LOG_SQL_LEVEL``：SQLAlchemy 日志级别，默认 ``WARNING``，设为 ``INFO`` 可打印所有 SQL

输出约定：

- 默认只写 stdout，交由 systemd / Docker / journald 收集与轮转，避免多进程写同一文件；
- 标准库的文件 handler 在 gunicorn 多 worker 下轮转不是进程安全的，多进程部署如需写文件，
  请改用 ``concurrent-log-handler`` 的 ``ConcurrentRotatingFileHandler``；
- 每条日志都会带上当前请求的 ``request_id``（优先复用上游传入的 ``X-Request-ID``），
  并在响应头中回写，便于跨服务串联一次请求的全部日志。
"""

import logging
import logging.config
import os
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from weakref import WeakSet

from flask import Flask, Response, request

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_VALID_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})

# 请求级别的上下文，随每次请求设置；非请求上下文（如定时任务）取默认值
_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_request_start: ContextVar[float] = ContextVar("request_start", default=0.0)

_access_logger = logging.getLogger("myapp.request")

# 已注册过请求钩子的 app，避免同一实例重复调用 setup_logging 时 access log 重复输出
_configured_apps: WeakSet[Flask] = WeakSet()


class RequestIdFilter(logging.Filter):
    """把当前请求的 request_id 注入每条日志记录，非请求上下文填充 ``-``"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        return True


def sanitize_log_value(value: object, max_length: int = 64) -> str:
    """清洗要写入日志的外部输入（用户名、QQ 号等）。

    只保留可打印字符，剔除换行/制表符等控制字符，避免外部输入伪造出额外的日志行；
    同时限长。空值或清洗后为空时返回 ``-``。
    """
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:max_length] or "-"


def _sanitize_token(value: str, max_length: int = 32) -> str:
    """token 类输入（如 request id）额外去掉空白字符"""
    text = "".join(ch for ch in value if ch.isprintable() and not ch.isspace())
    return text[:max_length]


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _normalize_level(raw: str, default: str = "INFO") -> str:
    """把环境变量里的级别名规范化，非法值回退到 default"""
    level = raw.strip().upper()
    return level if level in _VALID_LEVELS else default


def _build_config(level: str, log_dir: str, sql_level: str) -> dict[str, object]:
    handlers: dict[str, object] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": level,
            "formatter": "standard",
            "stream": "ext://sys.stdout",
            "filters": ["request_id"],
        }
    }
    handler_names = ["console"]

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "level": level,
            "formatter": "standard",
            "filename": str(Path(log_dir) / "app.log"),
            "when": "midnight",
            "backupCount": _env_int("LOG_FILE_BACKUP_DAYS", 7),
            "encoding": "utf-8",
            "delay": True,
            "filters": ["request_id"],
        }
        handler_names.append("file")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"request_id": {"()": "myapp.logging_config.RequestIdFilter"}},
        "formatters": {"standard": {"format": _LOG_FORMAT, "datefmt": _DATE_FORMAT}},
        "handlers": handlers,
        "loggers": {
            # 应用自身的日志统一挂在 myapp 命名空间下，不再向上传播，避免与 root 重复输出
            "myapp": {"level": level, "handlers": handler_names, "propagate": False},
            # SQLAlchemy 在 INFO 级别会打印所有 SQL，默认压低到 WARNING
            "sqlalchemy": {"level": sql_level, "handlers": handler_names, "propagate": False},
            # werkzeug 开发服务器自带的访问日志由下方 access log 统一接管
            "werkzeug": {"level": "WARNING", "handlers": handler_names, "propagate": False},
        },
        "root": {"level": level, "handlers": handler_names},
    }


def _assign_request_id() -> None:
    """为每个请求生成 request_id（优先复用上游传入值）并记录起始时间"""
    raw = request.headers.get("X-Request-ID", "")

    _request_id.set(_sanitize_token(raw) or uuid.uuid4().hex[:12])
    _request_start.set(time.perf_counter())


def _log_access(response: Response) -> Response:
    """记录访问日志并把 request_id 回写到响应头"""
    started_at = _request_start.get()
    request_id = _request_id.get()

    if started_at > 0.0 and request.method != "OPTIONS":
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        _access_logger.info(
            "%s %s %s %.1fms",
            request.method,
            request.full_path.rstrip("?"),
            response.status_code,
            elapsed_ms,
        )

    response.headers["X-Request-ID"] = request_id
    return response


def setup_logging(app: Flask) -> None:
    """按环境变量初始化全局日志，并注册请求级访问日志"""
    level = _normalize_level(_env_str("LOG_LEVEL", "INFO"))
    sql_level = _normalize_level(_env_str("LOG_SQL_LEVEL", "WARNING"), "WARNING")
    log_dir = _env_str("LOG_DIR")

    logging.config.dictConfig(_build_config(level, log_dir, sql_level))

    # 常规情况下 app.logger 即名为 myapp 的 logger；测试中临时创建的 Flask 实例名称可能不同，
    # 此时让它复用同一批 handler，避免出现第二套输出格式
    app_logger = app.logger
    if app_logger.name != "myapp":
        app_logger.handlers.clear()
        app_logger.handlers.extend(logging.getLogger("myapp").handlers)
        app_logger.propagate = False
    app_logger.setLevel(level)

    if app not in _configured_apps:
        _configured_apps.add(app)
        app.before_request(_assign_request_id)
        app.after_request(_log_access)
