"""单元测试：登录/注册/退出的审计日志（不区分账号存在性与密码错误的对外响应，但日志中要区分）"""

import logging
from collections.abc import Iterator

import pytest
from flask import Flask

from myapp import db
from myapp.auth import create_token
from myapp.db_model import User, UserRole, UserStatus

# 用独立 IP 规避登录限速（limiter 使用进程内共享存储，会在用例间累计计数）
TEST_IP = "203.0.113.77"


class _ListHandler(logging.Handler):
    """收集日志记录，便于断言"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def auth_logs() -> Iterator[_ListHandler]:
    """捕获 myapp.auth 的日志（该 logger 不向 root 传播，需直接挂 handler）"""
    handler = _ListHandler()
    logger = logging.getLogger("myapp.auth")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@pytest.fixture()
def active_user(app: Flask) -> User:
    user = User(username="loguser", user_qq="111222333", role=UserRole.USER, status=UserStatus.ACTIVE)
    user.password = "Abc12345!"
    db.session.add(user)
    db.session.commit()
    return user


def _messages(handler: _ListHandler) -> list[str]:
    return [record.getMessage() for record in handler.records]


def _levels(handler: _ListHandler) -> list[int]:
    return [record.levelno for record in handler.records]


class TestLoginLogging:
    def test_success_logs_username_and_ip(self, app: Flask, active_user: User, auth_logs: _ListHandler) -> None:
        resp = app.test_client().post(
            "/auth/login",
            json={"username": "loguser", "password": "Abc12345!"},
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 0
        assert _levels(auth_logs) == [logging.INFO]
        message = _messages(auth_logs)[0]
        assert "登录成功" in message
        assert "user=loguser" in message
        assert TEST_IP in message
        assert "Abc12345!" not in message, "日志中不得出现密码"

    def test_wrong_password_logs_reason(self, app: Flask, active_user: User, auth_logs: _ListHandler) -> None:
        resp = app.test_client().post(
            "/auth/login",
            json={"username": "loguser", "password": "WrongPass1"},
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 1  # 对外统一提示，不暴露账号是否存在
        assert _levels(auth_logs) == [logging.WARNING]
        message = _messages(auth_logs)[0]
        assert "登录失败" in message
        assert "原因=密码错误" in message

    def test_unknown_user_logs_distinct_reason(self, app: Flask, auth_logs: _ListHandler) -> None:
        app.test_client().post(
            "/auth/login",
            json={"username": "ghost", "password": "WrongPass1"},
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert "原因=用户不存在" in _messages(auth_logs)[0]

    def test_username_with_newline_stays_on_one_line(self, app: Flask, auth_logs: _ListHandler) -> None:
        app.test_client().post(
            "/auth/login",
            json={"username": "evil\n2026-01-01 | INFO | fake log line", "password": "x"},
            headers={"X-Forwarded-For": TEST_IP},
        )

        message = _messages(auth_logs)[0]
        assert "\n" not in message
        assert "fake log line" in message  # 内容保留但被压成同一行


class TestRegisterLogging:
    def test_success_logs_user_and_ip(self, app: Flask, auth_logs: _ListHandler) -> None:
        resp = app.test_client().post(
            "/auth/register",
            json={
                "username": "newloguser",
                "userQQ": "555666777",
                "password": "Abc12345!",
                "passwordAgain": "Abc12345!",
            },
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 0
        assert _levels(auth_logs) == [logging.INFO]
        message = _messages(auth_logs)[0]
        assert "注册成功" in message
        assert "user=newloguser" in message
        assert "qq=555666777" in message
        assert TEST_IP in message
        assert "Abc12345!" not in message, "日志中不得出现密码"

    def test_duplicate_qq_logs_reason(self, app: Flask, active_user: User, auth_logs: _ListHandler) -> None:
        resp = app.test_client().post(
            "/auth/register",
            json={
                "username": "another",
                "userQQ": active_user.user_qq,
                "password": "Abc12345!",
                "passwordAgain": "Abc12345!",
            },
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 3
        assert "原因=QQ 号已存在" in _messages(auth_logs)[0]

    def test_weak_password_logs_reason(self, app: Flask, auth_logs: _ListHandler) -> None:
        resp = app.test_client().post(
            "/auth/register",
            json={
                "username": "weakpass",
                "userQQ": "888999000",
                "password": "12345678",
                "passwordAgain": "12345678",
            },
            headers={"X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 2
        assert "原因=密码不合法" in _messages(auth_logs)[0]


class TestLogoutLogging:
    def test_success_logs_username(self, app: Flask, active_user: User, auth_logs: _ListHandler) -> None:
        token = create_token(active_user)

        resp = app.test_client().post(
            "/auth/logout",
            headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": TEST_IP},
        )

        assert resp.get_json()["code"] == 0
        assert _levels(auth_logs) == [logging.INFO]
        message = _messages(auth_logs)[0]
        assert "退出成功" in message
        assert "user=loguser" in message
        assert TEST_IP in message
