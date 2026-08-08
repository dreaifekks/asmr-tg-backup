# asmr-tg-backup

`asmr-tg-backup` 使用 `yt-dlp` 发现并归档公开的 YouTube、Twitch 与 RSS 媒体，
并可把生成的媒体投递到 Telegram。它以单个常驻进程运行，使用 SQLite 保存发现、
下载、投递及控制面板状态。

本应用使用 Telegram API。Telegram 投递始终使用用户自己的 bot token 与目标地址；
token 不会包含在安装包或镜像中。

## 选择安装方式

| 目标 | 从这里开始 |
| --- | --- |
| 带引导式初始化的轻量原生 Linux 服务 | [PyPI 与原生 Linux](getting-started/pypi.md) |
| 使用持久化 `/data` 的可复现容器 | [Docker Compose](getting-started/docker-compose.md) |
| 查看全部配置字段 | [参考](reference.md) |

官方 PyPI 与 GHCR 发行包在提供 bot token 和目标地址后，即可使用默认的 MTProto
媒体上传。源码构建也可以使用 MTProto，但需要成对提供自己的 Telegram application
ID/hash。

## Telegram 投递方式

安装方式和上传 transport 是两个独立选择：

- **MTProto 直接上传**是默认路径，不需要额外运行 Bot API 服务；可复用 session
  保存在私有数据目录中。
- **已有 Bot API URL**适合已经运行可信端点的用户。
- **本地 Bot API**是高级选项，可由原生 systemd 或 Compose 运行。
- **官方 Bot API 分块**是最后托底。超过 49 MB 安全阈值的音频会转换成可独立播放
  的分段，每一段都有独立标题和封面。

修改 transport 或上传大小前，请先阅读 [Telegram 投递](configuration/telegram.md)。

## 运行要求

- 原生安装需要 Python 3.11 或更高版本；
- 音频提取、封面、分段与直播录制需要 `ffmpeg` 和 `ffprobe`；
- Bot API transport 与 Telegram 控制面板需要 `curl`；
- 启用 Telegram 投递时需要 bot token 和目标地址；
- 只有启用 Twitch 发现时才需要 Twitch 凭据。

Compose 镜像已经包含系统媒体工具和 `cryptg` 加速。原生安装需要单独安装系统工具，
对于较大的 MTProto 上传，可以通过可选的 `performance` extra 安装 `cryptg`。

## 安全启动

配置与数据库初始化成功前，请保持来源和 Telegram 投递关闭。之后先启用一个来源并
验证一份归档，再启用投递。配置、环境文件、SQLite 数据库与 MTProto session 都应
保持私密。
