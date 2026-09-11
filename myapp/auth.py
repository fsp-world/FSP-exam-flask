import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from flask import Blueprint, current_app, jsonify, request
from flask_login import (
    current_user,
    login_required,  # type: ignore[reportUnknownVariableType]
    login_user,  # type: ignore[reportUnknownVariableType]
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from myapp import APP, db, limiter
from myapp.db_model import ActivationToken, RegistrationLimit, ResetPasswordToken, Token, User, UserStatus
from myapp.logging_config import sanitize_log_value
from myapp.mail import activation_mail, reset_password_mail, send_mail
from myapp.utils import is_password_complexity_valid, validate_username

auth = Blueprint("auth", __name__)

# JWT 签发方标识
JWT_ISSUER = "fsp-exam"

logger = logging.getLogger(__name__)


def _client_ip() -> str:
    """客户端 IP：优先 X-Forwarded-For 第一个值（适配反向代理），否则用 remote_addr"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


@auth.route("/login", methods=["POST"])
@limiter.limit("5 per minute")  # 登录限速：同一 IP 每分钟最多 5 次尝试
@limiter.limit("20 per hour")  # 同一 IP 每小时最多 20 次尝试
def login():
    req_data: dict[str, str] | None = request.json
    if req_data:
        username = req_data["username"]
        password = req_data["password"]
        stmt = select(User).where(User.username == username)
        user = db.session.scalar(stmt)
        if user and user.check_password(password):
            login_user(user)
            token = create_token(user)

            temp = current_user.whitelist
            play_permission: bool = True if len(temp) > 0 else False

            logger.info("登录成功 user=%s uid=%s ip=%s", sanitize_log_value(user.username), user.id, _client_ip())
            return jsonify(
                {
                    "code": 0,
                    "token": token,
                    "username": user.username,
                    "avatar": user.avatar,
                    "isAdmin": user.role == "admin",
                    "play_permission": play_permission,
                }
            )
        else:
            # 返回给前端不区分具体原因（避免账号枚举），日志中区分以便排查
            reason = "用户不存在" if user is None else "密码错误"
            logger.warning("登录失败 user=%s ip=%s 原因=%s", sanitize_log_value(username), _client_ip(), reason)
            return jsonify({"code": 1, "desc": "用户名或密码错误!"})
    logger.warning("登录失败 ip=%s 原因=请求字段错误", _client_ip())
    return jsonify({"code": 1, "desc": "字段错误！"})


@auth.route("/logout", methods=["POST"])
@login_required
def logout():
    if current_user.is_authenticated:
        # 用户已登录
        token: str | None = request.headers.get("Authorization")
        if token and token.startswith("Bearer "):
            token = token.replace("Bearer ", "", 1)

        stmt = select(Token).where(Token.token == token)
        tk = db.session.scalar(stmt)
        if tk is None:
            logger.warning(
                "退出失败 user=%s ip=%s 原因=token 不存在",
                sanitize_log_value(current_user.username),
                _client_ip(),
            )
            return jsonify({"code": 4, "desc": "Token not found"})

        # commit 后实例属性会过期，先取出来避免多一次查询
        username = sanitize_log_value(current_user.username)
        user_id = current_user.id

        db.session.delete(tk)
        db.session.commit()

        logger.info("退出成功 user=%s uid=%s ip=%s", username, user_id, _client_ip())
        return jsonify({"code": 0, "desc": "退出成功"})
    return jsonify({"code": 1, "desc": "error"})


@auth.route("/register", methods=["POST"])
def register():
    req_data = request.json
    if not req_data:
        logger.info("注册被拒绝 ip=%s 原因=请求数据错误", _client_ip())
        return jsonify({"code": 1, "desc": "请求数据错误"})

    # 获取客户端IP
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if client_ip is None:
        logger.warning("注册被拒绝 ip=unknown 原因=无法获取用户IP")
        return jsonify({"code": 1, "desc": "无法获取用户IP"})

    client_ip_split = client_ip.split(",")[0]

    # 检查IP注册限制
    if check_ip_registration_limit(client_ip_split):
        logger.warning("注册被拒绝 ip=%s 原因=该 IP 注册次数过多", client_ip_split)
        return jsonify({"code": 5, "desc": "该IP注册次数过多，请稍后再试!"})

    raw_username = req_data.get("username", "")
    user_qq = req_data.get("userQQ", "").strip()
    password = req_data.get("password", "").strip()
    re_password = req_data.get("passwordAgain", "").strip()

    # 验证必填字段
    if not all([raw_username, password, re_password]):
        logger.info("注册被拒绝 ip=%s 原因=表单字段缺失", client_ip_split)
        return jsonify({"code": 1, "desc": "表单错误!"})

    # 验证用户名
    username_result = validate_username(raw_username)
    if username_result["code"] != 0:
        logger.info(
            "注册被拒绝 user=%s ip=%s 原因=%s",
            sanitize_log_value(raw_username),
            client_ip_split,
            sanitize_log_value(username_result["desc"]),
        )
        return jsonify(username_result)
    username = username_result["username"]

    # 验证密码一致性
    if password != re_password:
        logger.info("注册被拒绝 user=%s ip=%s 原因=两次密码不一致", sanitize_log_value(username), client_ip_split)
        return jsonify({"code": 2, "desc": "密码与重复密码不一致!"})

    # 验证密码是否合法
    if not is_password_complexity_valid(password):
        logger.info("注册被拒绝 user=%s ip=%s 原因=密码不合法", sanitize_log_value(username), client_ip_split)
        return jsonify({"code": 2, "desc": "密码不合法!"})

    # 验证 QQ 号格式（QQ 号不能带邮箱后缀）
    if "@qq.com" in user_qq.lower():
        logger.info("注册被拒绝 user=%s ip=%s 原因=QQ 号带邮箱后缀", sanitize_log_value(username), client_ip_split)
        return jsonify({"code": 2, "desc": "请填写纯QQ号，不要带 @qq.com 后缀!"})

    # 验证 QQ 号
    stmt = select(User).where(User.user_qq == user_qq)
    existing_user = db.session.scalar(stmt)

    if existing_user:
        logger.info(
            "注册被拒绝 user=%s qq=%s ip=%s 原因=QQ 号已存在",
            sanitize_log_value(username),
            sanitize_log_value(user_qq),
            client_ip_split,
        )
        return jsonify({"code": 3, "desc": "QQ号已存在!"})

    # 创建用户
    try:
        new_user = User(
            username=username,
            user_qq=user_qq,
        )

        new_user.password = password

        db.session.add(new_user)

        # 记录IP注册信息
        record_ip_registration(client_ip)

        db.session.commit()

        logger.info(
            "注册成功 user=%s uid=%s qq=%s ip=%s",
            sanitize_log_value(new_user.username),
            new_user.id,
            sanitize_log_value(new_user.user_qq),
            client_ip_split,
        )

        token = create_token(new_user)
        return jsonify(
            {
                "code": 0,
                "desc": "注册成功",
                "token": token,
                "username": new_user.username,
                "avatar": new_user.avatar,
                "isAdmin": new_user.role == "admin",
            }
        )
    except IntegrityError:
        db.session.rollback()
        logger.exception(
            "注册失败 user=%s qq=%s ip=%s 原因=数据库唯一约束冲突",
            sanitize_log_value(username),
            sanitize_log_value(user_qq),
            client_ip_split,
        )
        return jsonify({"code": 5, "desc": "注册失败，请稍后再试"})


@auth.route("/check", methods=["GET"])
def check_login():
    if current_user.is_authenticated:
        # 用户已登录，返回用户信息

        temp = current_user.whitelist
        play_permission: bool = True if len(temp) > 0 else False

        return jsonify(
            {
                "code": 0,
                "username": current_user.username,
                "avatar": current_user.avatar,
                "isAdmin": current_user.role == "admin",
                "play_permission": play_permission,
            }
        )
    else:
        # 用户未登录，返回未登录提示
        return jsonify(
            {
                "code": 1,
                "desc": "User is not logged in",
                "avatar": "b83565e6-b0d0-4265-bb4f-fdb5e8d00655",
            }
        )


@auth.route("/findPassword", methods=["POST"])
def find_password():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)

    if client_ip is None:
        return jsonify({"code": 1, "desc": "无法获取用户IP"})

    client_ip_split = client_ip.split(",")[0]

    # 检查IP注册限制
    if check_ip_registration_limit(client_ip_split):
        return jsonify({"code": 5, "desc": "该IP注册次数过多，请稍后再试!"})

    request_data = request.get_json(silent=True)

    if request_data is None:
        return jsonify({"code": 5, "desc": "缺少数据"})

    qq: str | None = request_data.get("userQQ")

    user = db.session.scalar(select(User).where(User.user_qq == qq))
    if user is None:
        return jsonify({"code": 4, "desc": "未找到用户!"})

    send_reset_password(user)
    return jsonify({"code": 0, "desc": "发送成功！请查找邮箱!"})


@auth.route("/findPassword", methods=["PUT"])
def find_password_set():
    token_str = request.args.get("token", "")
    stmt = select(ResetPasswordToken).where(ResetPasswordToken.token == token_str)
    token = db.session.scalar(stmt)
    if not token:
        return jsonify({"code": 4, "desc": "无效token!"})

    data = request.json
    if data:
        password = data.get("password")
        if not is_password_complexity_valid(password):
            return jsonify({"code": 2, "desc": "密码不合法!"})

        user: User | None = token.user_r_p_t
        if not user:
            return jsonify({"code": 4, "desc": "未找到用户!"})

        user.password = password
        db.session.delete(token)
        db.session.commit()

        return jsonify({"code": 0, "desc": "修改成功！"})
    return jsonify({"code": 2, "desc": "缺少数据"})


@auth.route("/reqActivation", methods=["post"])
@login_required
def req_activation():
    user = db.session.scalar(select(User).where(User.username == current_user.username))
    if user is None:
        return jsonify({"code": 4, "desc": "未找到用户!"}), 404

    if user.status == UserStatus.ACTIVE:
        return jsonify({"code": 2, "desc": "账户状态正常！不需要进行激活！"})

    if user.status != UserStatus.INACTIVE:
        return jsonify({"code": 2, "desc": "账户状态异常！无法进行激活！"})

    stmt = select(ActivationToken).where(
        ActivationToken.user_id == user.id,
        ActivationToken.expires_at >= datetime.now(UTC),
        ActivationToken.is_revoked.is_(False),
    )

    token = db.session.scalar(stmt)
    if token:
        return jsonify({"code": 1, "desc": "链接未过期请稍后尝试！"})

    send_activation_mail(user)
    return jsonify({"code": 0, "desc": "发送成功！请查找邮箱!"})


@auth.route("/activation", methods=["PUT"])
def activation():
    token_str: str = request.args.get("token", "")
    stmt = select(ActivationToken).where(ActivationToken.token == token_str)
    token = db.session.scalar(stmt)
    if not token:
        return jsonify({"code": 4, "desc": "无效token!"})

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"code": 2, "desc": "json 解析失败"})

    username: str | None = data.get("username")

    user: User | None = token.user_active
    if not user or user.username != username:
        return jsonify({"code": 4, "desc": "未找到用户!"})

    user.status = UserStatus.ACTIVE
    db.session.delete(token)
    db.session.commit()

    return jsonify({"code": 0, "desc": "激活成功！"})


def send_activation_mail(user: User):
    with APP.app_context():
        # 邮件 token 仅用于数据库查找（不解码/不验签），用不透明随机串即可，无需 JWT
        token = secrets.token_urlsafe(32)
        db.session.add(ActivationToken(user_id=user.id, token=token))
        db.session.commit()
        msg = activation_mail([f"{user.user_qq}@qq.com"], token)
        send_mail(APP, msg)


def send_reset_password(user: User):
    with APP.app_context():
        # 邮件 token 仅用于数据库查找（不解码/不验签），用不透明随机串即可，无需 JWT
        token = secrets.token_urlsafe(32)
        db.session.add(ResetPasswordToken(user.id, token))
        db.session.commit()
        msg = reset_password_mail([f"{user.user_qq}@qq.com"], token)
        send_mail(APP, msg)


def check_ip_registration_limit(ip: str):
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    stmt = select(func.count(RegistrationLimit.id)).where(
        RegistrationLimit.ip == ip, RegistrationLimit.register_time >= one_hour_ago
    )
    registrations = db.session.scalar(stmt) or 0

    return registrations >= 2


def record_ip_registration(ip: str):
    registration = RegistrationLimit(ip=ip)
    db.session.add(registration)


def generate_token(user: User, expires_in: int = 3600) -> str:
    """生成 JWT 令牌。

    expires_in：过期时间（秒），默认 3600 秒 = 1 小时。
    携带标准声明：user_id（主体）、iss（签发方）、iat（签发时间）、exp（过期时间）、jti（唯一标识）。
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "user_id": user.id,
        "iss": JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=expires_in),
        "jti": uuid.uuid4().hex,
    }
    secret_key: str = current_app.config["SECRET_KEY"]  # type: ignore[reportUnknownVariableType]
    token: str = jwt.encode(payload, secret_key, algorithm="HS256")  # type: ignore[reportUnknownMemberType]
    return token


def create_token(user: User, expires_in: int = 3600 * 24 * 7):
    # 默认是7天过期
    expires_in_minutes = expires_in / 60  # 秒转分钟
    token = generate_token(user, expires_in)
    new_token = Token(
        user_id=user.id, token=token, expires_at=(datetime.now(UTC) + timedelta(minutes=expires_in_minutes))
    )
    db.session.add(new_token)
    db.session.commit()
    return token


def revoke_token(token: str):
    token_record = db.session.scalar(select(Token).where(Token.token == token))
    if token_record:
        token_record.is_revoked = True
        db.session.commit()


def is_token_revoked(token: str) -> bool:
    """Token 是否已失效：数据库记录不存在（logout 删除）或标记了 is_revoked 均视为已吊销"""
    token_record = db.session.scalar(select(Token).where(Token.token == token))
    return token_record is None or token_record.is_revoked


def verify_token(token: str, secret_key: str) -> int:
    """验证 JWT 签名，并检查数据库中的吊销状态（与 request_loader 保持一致）。

    返回：user_id 有效；-1 过期；0 无效；2 已吊销/已删除
    """
    try:
        # 注意：pyjwt 的 decode 参数是 algorithms（复数），encode 才是 algorithm（单数）
        payload = jwt.decode(token, secret_key, algorithms=["HS256"])  # type: ignore[reportUnknownMemberType]
        user_id = payload["user_id"]
    except jwt.ExpiredSignatureError:
        return -1  # Token 过期
    except jwt.InvalidTokenError:
        return 0  # 无效 Token

    if is_token_revoked(token):
        return 2  # 已吊销/已删除
    return user_id
