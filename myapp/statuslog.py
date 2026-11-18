from datetime import UTC, datetime
from typing import cast

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user
from sqlalchemy import desc, select

from myapp import db
from myapp.db_model import StatusLog
from myapp.utils import parse_dt_to_iso_utc

statuslog = Blueprint("statuslog", __name__)

# 未登录用户最大查询条数
ANONYMOUS_MAX: int = 100
# 已登录用户按时间段查询的最大返回条数（一千万，实际是兜底上限）
MAX_RANGE_RECORDS: int = 10_000_000


def _parse_time_arg(raw: str | None) -> datetime | None:
    """解析 ISO 8601 时间参数，无时区信息按 UTC 处理；格式非法时抛 ValueError"""
    if not raw:
        return None
    dt = datetime.fromisoformat(raw)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@statuslog.route("/log", methods=["GET"])
def online_stats():
    """在线人数统计接口

    默认查询今天的数据，今天数据不满50条则查询最新的100条记录。
    指定 start_time / end_time 参数则返回该时间段内的数据（按时间正序）。
    时间段内记录超过上限时只返回最新的部分（上限见 ANONYMOUS_MAX / MAX_RANGE_RECORDS）。
    时间参数格式非法时返回 400。
    """
    try:
        start_time = _parse_time_arg(request.args.get("start_time", type=str))
        end_time = _parse_time_arg(request.args.get("end_time", type=str))
    except ValueError:
        return jsonify({"code": 1, "desc": "时间参数格式错误，请使用 ISO 8601 格式（如 2026-01-01T00:00:00Z）"}), 400

    is_anonymous = not current_user.is_authenticated
    SERVER_LAUNCH_DATE = cast(str, current_app.config["SERVER_LAUNCH_DATE"])

    # 指定了时间段参数：直接按时间段查询
    if start_time or end_time:
        # 倒序取该时间段内最新的记录，再反转为正序返回
        stmt = select(StatusLog).order_by(desc(StatusLog.create_time))

        if start_time:
            stmt = stmt.where(StatusLog.create_time >= start_time)

        if end_time:
            stmt = stmt.where(StatusLog.create_time <= end_time)

        stmt = stmt.limit(ANONYMOUS_MAX if is_anonymous else MAX_RANGE_RECORDS)

        records = list(db.session.execute(stmt).scalars().all())
        records.reverse()  # 正序返回
    else:
        # 默认：今天的数据
        now = datetime.now(UTC)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

        stmt = (
            select(StatusLog)
            .where(StatusLog.create_time.between(today_start, today_end))
            .order_by(StatusLog.create_time.asc())
        )
        if is_anonymous:
            stmt = stmt.limit(ANONYMOUS_MAX)

        today_records = list(db.session.execute(stmt).scalars().all())

        if len(today_records) >= 50:
            records = today_records
        else:
            # 今天不满足50条，查询最新100条（未登录时受 ANONYMOUS_MAX 限制）
            fallback_limit = min(100, ANONYMOUS_MAX) if is_anonymous else 100
            stmt = select(StatusLog).order_by(desc(StatusLog.create_time)).limit(fallback_limit)
            records = list(db.session.execute(stmt).scalars().all())
            records.reverse()  # 正序返回

    count = [r.player_count for r in records]
    date = [parse_dt_to_iso_utc(r.create_time) for r in records]

    return jsonify(
        {
            "code": 0,
            "data": {"total": len(records), "count": count, "date": date, "serverLaunchDate": SERVER_LAUNCH_DATE},
        }
    )
