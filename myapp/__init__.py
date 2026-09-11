import os
from datetime import UTC, datetime
from typing import cast

from dotenv import load_dotenv
from flask import Flask, Request, Response, jsonify, request
from flask_apscheduler import APScheduler
from flask_bcrypt import Bcrypt
from flask_cors import CORS
from flask_limiter import Limiter
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from myapp.logging_config import setup_logging


class Base(MappedAsDataclass, DeclarativeBase):
    pass


login_manager: LoginManager = LoginManager()
scheduler: APScheduler = APScheduler()
db = SQLAlchemy(model_class=Base)
migrate = Migrate()
bcrypt: Bcrypt = Bcrypt()
mail: Mail = Mail()
cors = CORS()


def _limiter_key_func() -> str:
    """限流 key：优先取 X-Forwarded-For 第一个值（适配反向代理），否则用 remote_addr"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


limiter: Limiter = Limiter(
    key_func=_limiter_key_func,
    default_limits=[],
    storage_uri="memory://",  # 内存存储，单进程小项目足够；多 worker 部署如需精确限速需换共享存储
)


APP: Flask


def create_app():
    load_dotenv()
    app = Flask(__name__)
    global APP
    APP = app  # type: ignore[reportConstantRedefinition]
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")  # 测试数据库
    app.config["SESSION_PROTECTION"] = None  # 禁用会话保护
    app.debug = os.getenv("GUNICORN_RUNNING") != "1"  # gunicorn 下强制关闭 debug 模式
    app.template_folder = "../templates"
    app.static_folder = "../static"

    setup_logging(app)

    login_manager.init_app(app)  # type: ignore[reportUnknownMemberType]

    db.init_app(app)

    scheduler.init_app(app)  # type: ignore[reportUnknownMemberType]

    from myapp.db_model import Token

    with app.app_context():
        db.create_all()

    from .config import Config

    Config(app, db)

    cors.init_app(
        app=app,
        resources={
            r"/*": {
                "origins": app.config["ALLOWED_ORIGINS"],
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                # iOS Safari/WebKit 的 CORS 预检会把 user-agent 列入 Access-Control-Request-Headers，
                # 白名单不含它会导致预检失败（仅 Safari 系报 "User-Agent not allowed"）。
                # 显式放行 User-Agent 以兼容 Safari（也可直接改用 "*"）。
                "allow_headers": ["Content-Type", "Authorization", "User-Agent"],
            }
        },
    )
    migrate.init_app(app, db)
    bcrypt.init_app(app)  # type: ignore[reportUnknownMemberType]
    mail.init_app(app)
    limiter.init_app(app)  # type: ignore[reportUnknownMemberType]

    # Flask debug 模式的 reloader 会启动两个进程，只在子进程中启动 scheduler 避免重复执行
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN"):
        import myapp.tasks  # type: ignore

        scheduler.start()

    # 导入蓝图
    from myapp.admin import admin
    from myapp.api import api
    from myapp.auth import auth
    from myapp.dashboard import dashboard
    from myapp.guarantee import guarantee
    from myapp.query import query
    from myapp.schematic import schematic
    from myapp.statuslog import statuslog
    from myapp.survey import survey
    from myapp.user import user

    # 注册蓝图
    app.register_blueprint(api, url_prefix="/api")
    app.register_blueprint(auth, url_prefix="/auth")
    app.register_blueprint(user, url_prefix="/user")
    app.register_blueprint(admin, url_prefix="/admin")
    app.register_blueprint(query, url_prefix="/query")
    app.register_blueprint(schematic, url_prefix="/schematic")
    app.register_blueprint(statuslog, url_prefix="/statuslog")
    app.register_blueprint(survey, url_prefix="/survey")
    app.register_blueprint(guarantee, url_prefix="/guarantee")
    app.register_blueprint(dashboard, url_prefix="/dashboard")

    # @app.route("/")
    # def hello():
    #     return "Hello world!\nHello Flask!"

    # 未授权的用户重定向到登录页面
    @login_manager.unauthorized_handler  # type: ignore[reportUnknownMemberType]
    def unauthorized():  # type: ignore[reportUnusedFunction]
        return jsonify({"code": 1, "desc": "用户未登录"})

    # 管理登录状态的，这个函数是在每次请求时被调用的，它需要从用户 ID 重新创建一个 User 对象
    # 这是因为 User 对象并不会在请求之间保持，所以我们需要在每次请求开始时重新创建它
    # 使用 request_loader 自定义加载逻辑
    @login_manager.request_loader  # type: ignore[reportUnknownMemberType]
    def load_user_from_request(request: Request):  # type: ignore[reportUnusedFunction]
        token: str | None = request.headers.get("Authorization")
        if token and token.startswith("Bearer "):
            token = token.replace("Bearer ", "", 1)
            stmt = select(Token).where(Token.token == token)
            token_record = db.session.scalar(stmt)
            if (
                token_record
                and not token_record.is_revoked
                and token_record.expires_at.replace(tzinfo=UTC) > datetime.now(UTC)
            ):
                return token_record.token_user

        return None

    # OPTIONS 预检请求统一返回 204（after_request 中会加 CORS 头）
    @app.before_request  # type: ignore[reportUnknownMemberType]
    def handle_options_preflight() -> Response | None:  # type: ignore[reportUnusedFunction]
        if request.method == "OPTIONS":
            return app.make_response(("", 204))

    # 流程：flask-cors 拿到你的 origins 列表，检查请求的 Origin 是否在其中
    # 如果匹配，就把那个具体的源写到 Access-Control-Allow-Origin 响应头里回给浏览器
    # flask-cors 对 404 等错误响应不会加 CORS 头，这里无条件补上
    @app.after_request  # type: ignore[reportUnknownMemberType]
    def add_cors_headers(response: Response) -> Response:  # type: ignore[reportUnusedFunction]
        allowed_origins = cast(list[str], app.config["ALLOWED_ORIGINS"])
        origin = request.headers.get("Origin", "")

        if origin in allowed_origins or "*" in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin

        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        # 注意：此处的硬编码列表会覆盖 flask-cors 的 allow_headers 配置，必须与上方 cors.init_app 保持一致。
        # iOS Safari/WebKit 预检会携带 user-agent，放行 User-Agent 才能通过预检。
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, User-Agent"
        return response

    return app
