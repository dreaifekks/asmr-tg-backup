# asmr-tg-backup

`asmr-tg-backup` 使用 `yt-dlp` 发现并归档 YouTube 频道投稿，以及 Twitch VOD/直播，
并可把文件投递到 Telegram。可选扩展可以增加 Niconico 等来源提供方，
也可以让指定类型的网络操作通过代理连接。调度、持久化状态、下载和投递仍由核心统一管理。

Telegram Panel 与来源 CLI 管理可编辑的 `sources.toml`。YouTube 和 Twitch 按钮默认
显示；启用来源扩展后，Panel 会自动增加该 provider 的按钮，RSS 则通过来源目录手工
配置。SQLite 保存来源目录的运行时镜像，以及内容发现、下载、Telegram 投递和控制面板
状态。

[查看 Telegram 展示频道](https://t.me/+9-Cy-yue1PJiMWY9){ target="_blank" rel="noopener noreferrer" }

## 从这里开始

- 需要轻量服务和引导式初始化时，使用 [PyPI 与原生 Linux](getting-started/pypi.md)。
- 需要持久化 `/data` 的可复现容器时，使用 [Docker Compose](getting-started/docker-compose.md)。
- 阅读[选择部署方式](getting-started/index.md)，比较两种路径并准备 bot、目标地址和
  管理员用户 ID。
- 完成核心安装与基础配置后，如果需要其他来源提供方或分域网络路由，再添加
  [可选扩展](configuration/extensions.md)。首次连接本身就依赖代理时，在启动服务前
  启用 `proxy-router`。

官方 PyPI 与 GHCR 版本默认通过 MTProto 上传，无需单独部署 Bot API；也可以改用已有、
本地或 Telegram 官方 Bot API。

## 文档内容

- [控制面板](configuration/control-panel.md)：推荐的来源与过滤器管理入口、状态和
  已跟踪文件删除。
- [来源与下载](configuration/sources.md)：完整来源字段、手工精调、内置与扩展来源，
  以及下载配置。
- [扩展](configuration/extensions.md)：一条命令启用、Panel 自动生成的 provider 按钮、
  私密配置和分域连接路由。
- [Telegram 投递](configuration/telegram.md)：transport、session、大小限制和连接
  配置。
- [运行与维护](operations.md)：命令、备份、更新和停止服务。
- [故障排查](troubleshooting.md)：常见初始化与运行问题。
- [参考](reference.md)：CLI、路径、配置区块和环境变量覆盖。
- [架构与开发](development.md)：运行边界和面向外部贡献者的开发流程。
- [参与贡献](contributing.md)：本地环境、测试、双语文档和提交前检查。
