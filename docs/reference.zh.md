# 参考

## 命令

| 命令 | 用途 |
| --- | --- |
| `asmr-tg-backup setup` | 选择 MTProto 或高级 Bot API 路线，创建私有配置并初始化 SQLite |
| `asmr-tg-backup init-config --output PATH` | 复制包内安全示例，拒绝覆盖已有文件 |
| `asmr-tg-backup init --config PATH` | 初始化目录和 SQLite |
| `asmr-tg-backup run --config PATH` | 持续运行轮询、worker、投递与控制面板 |
| `asmr-tg-backup poll --config PATH` | 运行一次发现并处理任务 |
| `asmr-tg-backup poll --no-process --config PATH` | 只发现和入队 |
| `asmr-tg-backup process --config PATH` | 不执行发现，只处理队列 |
| `asmr-tg-backup status --config PATH` | 输出队列与最近项目状态 |
| `asmr-tg-backup enqueue URL --config PATH` | 手动加入一个 YouTube URL |

### Setup 配置档

| 配置档 | 结果 |
| --- | --- |
| `mtproto-official` | 默认的官方发行包 MTProto 配置 |
| `mtproto-own` | 源码 setup 中输入私有 application ID/hash 的 MTProto 配置 |
| `custom-api-single` | 使用已有可信 Bot API URL 的大单文件配置 |
| `local-api-single` | 在 `127.0.0.1:18081` 注册预装本地 Bot API |
| `official-api-split` | 使用 `api.telegram.org`、49 MB 安全限制与可播放音频分块 |

## 原生路径

XDG 变量会替换对应的默认根目录。

| 资源 | XDG 路径 | 默认路径 |
| --- | --- | --- |
| Setup 配置 | `$XDG_CONFIG_HOME/asmr-tg-backup/config.toml` | `~/.config/asmr-tg-backup/config.toml` |
| 应用数据 | `$XDG_DATA_HOME/asmr-tg-backup` | `~/.local/share/asmr-tg-backup` |
| 数据库 | 应用数据目录下 | `~/.local/share/asmr-tg-backup/state.db` |
| 下载 | 应用数据目录下 | `~/.local/share/asmr-tg-backup/downloads` |
| MTProto session | 配置在应用数据目录下 | `~/.local/share/asmr-tg-backup/telegram-mtproto.session` |
| 本地 API env | `$XDG_CONFIG_HOME/asmr-tg-backup/telegram-bot-api.env` | `~/.config/asmr-tg-backup/telegram-bot-api.env` |
| 本地 API unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup-telegram-bot-api.service` | `~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service` |
| 本地 API 数据 | `$XDG_DATA_HOME/asmr-tg-backup/telegram-bot-api` | `~/.local/share/asmr-tg-backup/telegram-bot-api` |

Docker 设置 `ASMR_TG_BACKUP_DATA_DIR=/data`；命名卷 `asmr-data` 保存数据库、下载与
MTProto session。

## 配置区块

| 区块 | 用途 |
| --- | --- |
| `[app]` | 数据路径、轮询、重试、lease、worker 数量和日志 |
| `[[origins]]` | YouTube、Twitch 或 RSS provider 来源 |
| `[download]` | yt-dlp、ffmpeg、格式、路径、超时和 sidecar |
| `[download.provider_profiles.*]` | 各 provider 的下载覆盖 |
| `[telegram]` | 启用、token、目标、transport、媒体和 caption |
| `[telegram.mtproto]` | Application 凭据对、session 路径与 MTProto 大小限制 |
| `[telegram.bot_api]` | Bot API 地址、大小限制与可播放分块 |
| `[control]` | Telegram 面板权限与轮询 |
| `[twitch]` | Helix 凭据与 VOD/live 行为 |

## 环境变量

| 变量 | 覆盖或控制 |
| --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | `[app].data_dir` |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | `telegram.bot_api.api_base` |
| `TELEGRAM_MAX_UPLOAD_BYTES` | `telegram.bot_api.max_upload_bytes` |
| `TWITCH_CLIENT_ID` | Twitch client ID |
| `TWITCH_ACCESS_TOKEN` | 已有 Twitch app access token |
| `TWITCH_CLIENT_SECRET` | Twitch app-token 创建与刷新 |

两个 MTProto 变量必须一起出现。官方发行包中，完整运行时凭据对会覆盖发行默认值；
源码构建没有对应默认值，选择 MTProto 时需要自己的凭据对。

## 投递流程

```text
发现 -> 入队 -> 下载 -> 准备媒体
  -> 所选 transport 准备/上传
  -> 发送提交
  -> 保存 Telegram message ID
```

媒体根据 `upload_transport` 使用 MTProto 或 Bot API。只有选择 Bot API 且超过其字节
限制时，才执行音频分块。结果不明确的发送会进入 `uncertain`，不会换一种 transport
再次发送。

无论媒体使用哪种 transport，控制面板仍然是 Bot API consumer。

## 安全边界

- 配置、环境文件、SQLite 和 `.session` 文件都应保持私密。
- 绝不要把 bot token 或 session 写入包、镜像、issue 或日志。
- MTProto application 凭据必须完整来自同一来源，不能混用两边。
- 非回环 Bot API 地址使用 HTTPS。
- 本地 Bot API 和统计端点只绑定可信接口。
- 媒体出口代理与回环 Telegram API 流量保持隔离。
- 只有具备同等凭据保护能力的备份位置才可以保存 session。
