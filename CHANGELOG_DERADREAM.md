# DeraDream Fork Changelog

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
