# DeraDream Fork Changelog

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
