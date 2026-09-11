# Changelog

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
并且本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **用法**
>
> - 日常开发：把改动记录在顶部 `[Unreleased]` 区块，按下列变更类型归类。
> - 发布新版本：新建 `## [版本号] - YYYY-MM-DD` 区块（版本号遵循语义化版本，日期用 ISO 8601），把 `[Unreleased]` 中的内容整体移入。
> - 版本按时间倒序排列（新版本在前、旧版本在后）。

> **变更类型**
>
> - `Added`：新添加的功能
> - `Changed`：对现有功能的变更（含重构、样式及构建/工程配置调整）
> - `Deprecated`：即将移除、不建议继续使用的功能
> - `Removed`：已移除的功能或依赖
> - `Fixed`：Bug 修复
> - `Security`：安全性改进

## [Unreleased]

### Added

- 新增统一日志模块
    - 默认仅输出到 stdout 交由 systemd/Docker 收集
    - 登录成功/退出成功记 INFO、登录失败记 WARNING、注册成功记 INFO、注册被拒绝记 INFO/WARNING（含具体原因）...

### Fixed

- 修复管理后台编辑题目接口因前后端请求字段未对齐而无法获取问卷 ID 的问题（88606a9）

### Changed

- 统一前后端密码复杂性校验规则：密码长度为 8~16 个字符，且至少包含一个字母和一个数字；重命名密码校验函数并同步更新相关单元测试

## [0.2.5] - 2026-08-27

> **注意**：本次更新涉及严重问题，强烈建议立即升级
>
> 影响性：
>
> - 数据库：无
> - 前端：无

### Added

- 问卷审核通过后，考试结果邮件展示加群二维码；担保通过后，担保结果邮件同样展示加群二维码
- 拒绝时不再展示二维码，也不再附带无用的图片附件

### Changed

- 新增认证安全单元测试（发送环节通过 mock 屏蔽，无需配置邮件服务器），覆盖：`SECRET_KEY` 解析（环境变量优先 / 随机兜底 / 清理遗留记录）、登录接口限速（同一 IP 超限返回 429）、`verify_token` 与 `request_loader` 的吊销/删除/过期校验、JWT 标准声明与过期时间、邮件 token 格式
- 测试基建：sqlite 内存库兼容 MySQL `LONGTEXT` 类型，测试间自动清理业务表保证用例隔离

### Fixed

- 修复 iOS Safari/WebKit 的 CORS 预检失败问题：Safari 系浏览器的预检请求会在 `Access-Control-Request-Headers` 中携带 `User-Agent`，而 `after_request` 钩子硬编码的 `Access-Control-Allow-Headers` 白名单不含它导致预检被拒（报 "User-Agent not allowed"），现已放行 `User-Agent` (#13)

### Security

- 修复 `SECRET_KEY` 硬编码漏洞：不再在代码/数据库中内置固定默认密钥，改为优先通过环境变量 `SECRET_KEY` 注入；未设置时每次启动随机生成临时密钥（进程重启后所有登录态失效，生产/多 worker 部署必须设置固定环境变量）。密钥生成方式详见 `.env.example`
- 启动时自动清除数据库中历史版本硬编码的弱密钥记录
- 注意：更换 `SECRET_KEY` 后，此前签发的 JWT（登录态、邮件激活/重置密码链接）将失效，相关用户需重新登录或重新发送邮件
- 为登录接口添加基于 IP 的速率限制（同一 IP 每分钟最多 5 次、每小时最多 20 次登录尝试，超出返回 429），防止暴力破解
- 修复 `verify_token` 未校验吊销状态的问题：现在验签后会检查数据库中的 Token 记录，logout 删除或标记 `is_revoked` 的 Token 均视为已吊销，与 `request_loader` 行为保持一致
- 完善 JWT payload 设计：新增 `iss`（签发方）、`iat`（签发时间）、`jti`（唯一标识）标准声明；修复 `exp` 计算单位错误（原按「小时×24」计算导致过期时间被放大到数年，现改为按秒精确计算），使 JWT 自身过期时间与数据库 `expires_at` 保持一致
- 邮件激活/重置密码 token 不再使用 JWT，改用 `secrets.token_urlsafe(32)` 生成短小的不透明随机串（验证路径本就仅按 token 查库、不验签），链接更短且无法被解码
- 修复 `verify_token` 中 `jwt.decode` 参数错误（误用单数 `algorithm`，应为复数 `algorithms`），此前该函数实际无法完成验签、恒返回"无效 token"；该问题由新增单元测试发现并验证

## [0.2.4] - 2026-08-22

> **破坏性变更**：升级前请注意
>
> - 涉及数据库结构变更：新增 `responses.reject_reason` 字段，发布后需执行 `uv run flask --app main.py db upgrade` 迁移
> - 修复了已提交答卷被误标为超时的问题。若线上已有"已提交却被误标为超时"的答卷，需手动改回待审核：
>
>     ```sql
>     UPDATE responses SET is_reviewed = 0 WHERE is_completed = 1 AND is_reviewed = 3;
>     ```

### Added

- 答卷审核支持填写拒绝理由，理由将随邮件发送给用户（新增 `responses.reject_reason` 字段，涉及数据库结构变更）
- 支持关闭邮件提醒功能

### Changed

- 添加 github CI workflow
- 邮件时间统一为东八区显示（答卷完成时间、考试批改日期、担保处理日期）
- 统一全部邮件模板样式：所有邮件继承 `mail_base.html` 母版（含网站 logo、统一页脚），邮件主题与正文大标题统一由代码单点定义

### Fixed

- 修复答题问题
- 修复不支持题目分数批改为 0 分的问题
- 修复已提交答卷被误标记为超时的问题（仅"超时未提交"的答卷才标记为超时，已提交的保持待批改）
- 注册及管理后台添加/修改用户时，校验 QQ 号不允许带 `@qq.com` 邮箱后缀

## [0.2.3] - 2026-07-06

### Added

- 添加生产环境的数据库迁移脚本
- 默认配置添加'开服日期'项
- 默认配置添加'在线人数采集间隔'项

### Changed

- 生产环境用 gunicorn 跑的时候自动关闭 debug 模式
- 重构问卷和阅卷相关代码
- OPTIONS 预检请求统一返回 204

### Fixed

- 修复 `/admin/config/set` 的字段问题
- 修复 `/admin/survey/<int:sid>` 和 `/survey/survey/<int:sid>` 中图片列表的字段错误

## [0.2.2] - 2026-06-29

此次更新我们重构了大量的代码并引入单元测试，将全部代码替换为最新写法并添加类型提示，以保障代码质量。

### Added

- 添加 Profile 表、Statuslog 表
- 添加自定义背景接口
- 添加修改用户名接口
- 添加仪表盘相关接口
- 合并了来自 fsp-mcstatuslog 的功能：采集服务器玩家在线人数
- 答卷状态 Enum 添加超时状态

### Changed

- 重构 admin 接口
- 数据表类的基类从 `db.Model` 转为 `Base`，以获取更好的 pyright 等静态检查工具的支持
- 重构 ORM 调用，从 sqlalchemy1.0 写法全面转换为 sqlalchemy2.0 写法
- 为代码添加类型提示以提供更好的静态检查支持
- 使用 Enum 标注数据库字段

### Fixed

- 修复 default_config 字段错误
- 修复了开始考试之后, 更改 URL 地址可以访问其他问卷的问题，同时在提交答卷时也对问卷ID和服务器记录做比对 (#8)
- 修复了 Response 表的 submit_time 和 end_time 字段缺少空值处理的问题并修正字段类型标注 (#9)

## [0.2.1] - 2026-06-10

### Changed

- 重命名 responses 表的 response_time 字段为 submit_time
- 为 responses 表添加 end_time 字段
- 重命名 users 表的 addtime 字段为 registered_at
- 删除了 surveys 表的 status 字段

### Fixed

- 修复了答卷过期时间会动态变化的问题 (#2)
- 修复白名单用户信任链接口缺少必要属性的问题
- 修复初始化项目时默认管理员无法创建的问题 (#6)
- 修复其他问题

## [0.2.0] - 2026-06-05

### Added

- 添加投影相关接口

### Fixed

- 修复了一些问题
