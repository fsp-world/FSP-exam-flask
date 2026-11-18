"""单元测试：在线人数统计接口的时间段查询、参数校验与返回条数上限"""

from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask

from myapp import db
from myapp.db_model import StatusLog, Token, User, UserRole, UserStatus
from myapp.statuslog import ANONYMOUS_MAX, MAX_RANGE_RECORDS

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
RANGE_QUERY = "/statuslog/log?start_time=2025-12-01T00:00:00Z&end_time=2026-02-01T00:00:00Z"


def _add_logs(count: int, start: datetime = BASE_TIME, step: timedelta = timedelta(minutes=1)) -> None:
    """写入 count 条按 step 递增的在线记录，player_count 依次为 0..count-1"""
    db.session.add_all(
        [StatusLog(server_address="127.0.0.1", player_count=i, create_time=start + step * i) for i in range(count)]
    )
    db.session.commit()


def _auth_headers() -> dict[str, str]:
    """创建一个持有有效 Bearer token 的用户，返回请求头"""
    user = User(username="loguser", user_qq="123456789", role=UserRole.USER, status=UserStatus.ACTIVE)
    user.password = "Abc12345!"
    db.session.add(user)
    db.session.flush()
    db.session.add(Token(user_id=user.id, token="test-log-token"))
    db.session.commit()
    return {"Authorization": "Bearer test-log-token"}


class TestTimeParamValidation:
    """非法时间参数应返回 400 而不是 500"""

    def test_invalid_start_time(self, app: Flask) -> None:
        resp = app.test_client().get("/statuslog/log?start_time=not-a-date")
        assert resp.status_code == 400
        assert resp.get_json()["code"] == 1

    def test_invalid_end_time(self, app: Flask) -> None:
        resp = app.test_client().get("/statuslog/log?end_time=2026-13-45T00:00:00")
        assert resp.status_code == 400
        assert resp.get_json()["code"] == 1

    def test_empty_params_still_succeed(self, app: Flask) -> None:
        resp = app.test_client().get("/statuslog/log?start_time=&end_time=")
        assert resp.status_code == 200
        assert resp.get_json()["code"] == 0


class TestRangeQuery:
    """指定时间段时按时间正序返回"""

    def test_returns_ascending_records_in_range(self, app: Flask) -> None:
        _add_logs(5)

        data = app.test_client().get(RANGE_QUERY).get_json()["data"]

        assert data["count"] == [0, 1, 2, 3, 4]
        assert data["date"] == sorted(data["date"])
        assert data["total"] == 5

    def test_records_outside_range_excluded(self, app: Flask) -> None:
        _add_logs(2, start=BASE_TIME - timedelta(days=60))
        _add_logs(2, start=BASE_TIME)

        data = app.test_client().get(RANGE_QUERY).get_json()["data"]

        assert data["count"] == [0, 1]

    def test_naive_time_treated_as_utc(self, app: Flask) -> None:
        _add_logs(3)

        resp = app.test_client().get("/statuslog/log?start_time=2026-01-01T00:00:00&end_time=2026-01-01T00:02:00")

        assert resp.get_json()["data"]["count"] == [0, 1, 2]


class TestRangeQueryLimit:
    """时间段查询必须有条数上限，避免一次拉取整张表"""

    def test_anonymous_limited_to_latest_anon_max(self, app: Flask) -> None:
        _add_logs(ANONYMOUS_MAX + 20)

        data = app.test_client().get(RANGE_QUERY).get_json()["data"]

        assert data["total"] == ANONYMOUS_MAX
        assert data["count"] == list(range(20, ANONYMOUS_MAX + 20))
        assert data["date"] == sorted(data["date"])

    def test_authenticated_limited_by_max_range_records(self, app: Flask, monkeypatch: pytest.MonkeyPatch) -> None:
        # 真实上限一千万，测试中无法构造这么多数据，缩小上限验证截断逻辑
        monkeypatch.setattr("myapp.statuslog.MAX_RANGE_RECORDS", 5)
        headers = _auth_headers()
        _add_logs(25)

        data = app.test_client().get(RANGE_QUERY, headers=headers).get_json()["data"]

        assert data["total"] == 5
        assert data["count"] == list(range(20, 25))

    def test_max_range_records_is_ten_million(self) -> None:
        assert MAX_RANGE_RECORDS == 10_000_000


class TestDefaultQuery:
    """不带时间参数时仍返回今天的数据"""

    def test_returns_today_records(self, app: Flask) -> None:
        _add_logs(3, start=datetime.now(UTC) - timedelta(minutes=3))

        data = app.test_client().get("/statuslog/log").get_json()["data"]

        assert data["count"] == [0, 1, 2]
