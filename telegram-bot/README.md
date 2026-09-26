# ImgBed Telegram Bot

此目录是 DeraDream/CloudFlare-ImgBed fork 的 Telegram 上传入口。

设计目标：

- Telegram **只作为上传端**，不再作为存储后端。
- 不需要图床登录，不提供 `/login`。
- Bot 使用一个仅具备上传权限的 ImgBed API Token。
- 使用 `ALLOWED_USER_IDS` 限制可操作 Telegram 用户。
- 可在 Telegram 内配置与 Web 上传页对应的常用上传参数：
  - 存储渠道 / 渠道名称
  - 上传目录
  - 自动切换
  - 文件命名方式
  - 转换 WebP
  - 图片压缩
  - 压缩阈值
  - 期望大小
- 支持 Telegram Photo 和 Document。
- 上传成功返回 URL / Markdown / BBCode。
- 用户设置保存到 SQLite。

## 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 是 | BotFather 创建的 Telegram Bot Token |
| `IMGBED_API_TOKEN` | 是 | CloudFlare-ImgBed API Token，至少有 `upload` 权限 |
| `ALLOWED_USER_IDS` | 是 | 允许使用 Bot 的 Telegram User ID，多个用逗号分隔 |
| `IMGBED_URL` | 否 | Bot 调用的内部地址，Compose 默认 `http://imgbed:8080` |
| `IMGBED_PUBLIC_URL` | 建议 | 返回给用户的公网图床地址，例如 `https://img.example.com` |
| `BOT_DB_PATH` | 否 | SQLite 路径，默认 `/data/bot.db` |
| `REQUEST_TIMEOUT` | 否 | 上传请求超时，默认 120 秒 |

## 命令

- `/start`：开始使用
- `/settings`：完整上传设置
- `/set_storage`：选择默认存储渠道
- `/me`：查看当前 Bot 用户和上传配置

Bot 会调用 `GET /api/channels` 动态读取图床中已经配置并启用的渠道，并主动过滤 Telegram 存储类型。

## Web 后台配置 Bot

Docker 版可以直接在 **系统设置 → 上传设置 → Telegram Bot 上传** 卡片中点击「编辑」配置：

- **Bot Token**：BotFather 生成的 Token
- **允许用户 ID**：允许使用 Bot 的 Telegram User ID，多个用逗号分隔
- **ImgBed API Token**：建议使用仅含 `upload` 权限的 ImgBed API Token
- **启用开关**：打开后 Bot 容器会自动检测配置并开始运行

Telegram 不会出现在「添加存储渠道」或首页「上传渠道」中，它只作为上传入口。

环境变量 `BOT_TOKEN`、`IMGBED_API_TOKEN`、`ALLOWED_USER_IDS` 仍可作为高级覆盖项；若留空，则以 Web 后台配置为准。

## 图片预处理

WebP 和压缩在 Bot 侧完成，然后再调用 ImgBed `/upload`：

- JPEG / WebP：在超过阈值时按目标大小进行质量压缩。
- PNG：只做无损 optimize；需要明显减小时建议打开 WebP。
- GIF：为避免破坏动画，保持原文件。

## 致谢

Telegram 交互设计参考了 lhl77/ImgTGBot 的使用体验，但本实现为 CloudFlare-ImgBed API 独立编写。
