# DeraDream Fork Changelog

## 3.2

- 修复单容器镜像升级到 Alpine / Python 3.14 后 Telegram Bot 启动即崩溃的问题。
- Python 3.14 不再为主线程隐式创建 asyncio event loop，Bot 现在会在启动 polling 前显式创建并绑定事件循环。
- 修复症状：`RuntimeError: There is no current event loop in thread 'MainThread'`。
- Bot 版本更新为 v0.4.3。

## 3.1

- Docker 部署改为单容器：ImgBed、Telegram Bot、在线更新器全部运行在同一个 `imgbed` 容器中。
- 使用 Supervisor 管理 Node 主服务、Telegram Bot 和 updater 三个进程；任一子进程异常会在容器内自动拉起。
- Compose 永久服务从 3 个减少为 1 个，不再存在 `telegram-bot` / `updater` 服务 DNS 丢失问题。
- Bot 内部调用 ImgBed 改为 `127.0.0.1:8080`，调用 updater 改为 `127.0.0.1:8081`。
- 保留原 `./telegram-bot-data` 挂载，历史记录、设置、Session 与待续传任务无需迁移。
- 在线升级只构建一个 ImgBed 一体化镜像，再通过临时 helper 容器原子重建唯一的 `imgbed` 服务。
- 新 updater 会校验运行中的容器镜像 ID，避免旧镜像仍在运行时误报升级成功。
- 首次从旧三容器架构迁移时使用 `docker compose up -d --build --force-recreate --remove-orphans`，旧 Bot/updater 容器会自动移除。

## 3.0

- Telegram Bot 支持网页「上传密码」认证兜底。
- 当上传接口返回 401 时，不再直接显示失败；Bot 会明确提示用户输入上传密码。
- Bot 不保存明文上传密码：认证成功后仅缓存服务端返回的 `user_session`。
- 用户发送的密码消息会尽量立即从 Telegram 私聊中删除。
- 单图、视频、文件、相册批量任务在认证成功后会自动继续，无需重新发送媒体。
- 当上传密码被修改、用户会话过期或服务端主动失效会话后，下次 401 会自动清除旧会话并重新要求认证。
- 保留原有 ImgBed API Token 认证；具有 `upload` 权限的 Token 仍可直接上传，无需输入上传密码。
- Bot 版本更新为 v0.4.2。

## 2.9

- 修复在线升级在 deploy 阶段后 updater 服务永久消失的问题。
- updater 不再在自身容器内直接执行 Compose 重建自己，避免进程被自己杀死后留下半重建容器和名称冲突。
- updater 自更新改为独立临时 helper 容器完成，当前 updater 被替换后 helper 仍可继续把新 updater 拉起。
- Telegram Bot 查询 updater 时增加短暂重启容错，最多重试约 10 秒，避免正常自更新窗口被误报为“更新服务不可用”。
- Bot 版本更新为 v0.4.1。

## 2.8

- Telegram Bot 新增「🕘 最近上传」历史记录与分页详情。
- 新增 Telegram 相册 Media Group 批量上传，统一显示整体进度与结果汇总。
- 新增按日期自动目录，可在基础目录后追加 `YYYY/MM/DD`。
- 上传进度增强：动态进度条 + 百分比，并在下载/上传阶段显示速度与 ETA。
- 上传失败新增「🔄 重新上传」，保存 Telegram file_id 与上传设置快照后可一键重试。
- 新增 SHA-256 查重 / 秒传，同一存储渠道与目录命中历史文件时直接复用 URL。
- 上传成功新增媒体信息卡片：尺寸、格式、最终/原始大小、存储渠道、目录等。
- 新增标签 / 备注：Caption 可写 `#标签 备注`，上传后也可编辑。
- 标签、备注、查重记录和最近上传历史存储在 Bot SQLite，不要求提高 ImgBed API Token 权限。
- Bot 版本更新为 v0.4.0。

## 2.7

- Telegram 上传成功结果移除 BBCode，仅保留 URL 与 Markdown。
- Bot 版本更新为 v0.3.3。

## 2.6

- 版本号改为两段式短版本，例如 `2.6`，不再显示 `2.7.6-dd.x`。
- Telegram 版本检查不再显示 Git Commit 哈希。
- 修复 updater 重启后遗留 `updating=true` 导致一直卡在“正在应用新版本”的问题。
- updater 可自动识别“状态文件显示升级中，但当前进程没有升级线程”的情况，并恢复为成功或失败状态。
- Bot 版本更新为 v0.3.2。

## 2.7.6-dd.5

- Telegram 输入上传目录、压缩阈值、期望大小后立即返回“设置成功”反馈。
- 上传目录允许直接输入 `/telegram`、`/images` 等以斜杠开头的值，不再被 Telegram 当作未知命令吞掉。
- 版本检查在已是最新版时明确提示“当前已经是最新版本，无需升级”。
- 升级 deploy 阶段文案改为“正在重启 ImgBed 与 Telegram Bot 并应用新版本”，避免“切换”含义不清。
- Bot 版本更新为 v0.3.1。

## 2.7.6-dd.4

- Telegram 内触发升级后自动持续跟踪升级状态。
- 升级成功或失败都会主动反馈到原升级消息，不需要再次点击「版本升级」。
- Bot 在升级过程中被重启后，可从持久化状态自动续接并反馈最终结果。
- updater 重启后恢复最后一次升级状态。
- 图片、视频和文件上传新增百分比进度条。
- 上传进度覆盖 Telegram 下载、图片预处理、上传图床、生成链接等阶段。
- 新增 Telegram Video 消息直接上传支持。
- Bot 版本更新为 v0.3.0。

## 2.7.6-dd.3

- 移除 Bot 自定义「⌨️ 收起菜单」按钮。
- Reply Keyboard 仅保留功能按钮。
- 展开/收起完全交给 Telegram 客户端原生键盘按钮控制。
- Telegram Bot 版本更新为 v0.2.2。

## 2.7.6-dd.1

基于上游 CloudFlare-ImgBed 2.7.6 的定制版本。

- Telegram 不再作为存储渠道。
- Telegram 改为独立上传入口。
- Telegram Bot 支持图片/文件上传、渠道选择、目录、命名、WebP、压缩设置。
- Telegram Bot 配置集成到 Web 后台，不需要图床登录。
- 使用 Telegram Reply Keyboard 快捷按钮；移除左侧命令菜单。
- 新增 Web / Telegram 在线版本检查与一键升级。
- 新增 Docker 内部 updater 服务，更新源固定为 DeraDream/CloudFlare-ImgBed main。
- 保留原有本地 R2_env、S3 / Cloudflare R2、WebDAV、Hugging Face、Discord 存储。
