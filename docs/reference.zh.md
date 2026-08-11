# 参考

## 命令

| 命令 | 用途 |
| --- | --- |
| `asmr-tg-backup setup` | 选择 MTProto 或高级 Bot API 路线，创建私有配置、来源目录并初始化 SQLite |
| `asmr-tg-backup service install [--config PATH]` | 生成、启用并启动 systemd 用户服务，同时开启 linger 以便开机启动 |
| `asmr-tg-backup service uninstall` | 停止并移除生成的 unit，保留全部应用数据 |
| `asmr-tg-backup init-config --output PATH` | 复制包内安全示例，拒绝覆盖已有文件 |
| `asmr-tg-backup init --config PATH` | 初始化目录和 SQLite |
| `asmr-tg-backup run --config PATH` | 持续运行轮询、worker、投递与控制面板 |
| `asmr-tg-backup poll --config PATH` | 运行一次发现并处理任务 |
| `asmr-tg-backup poll --no-process --config PATH` | 只发现和入队 |
| `asmr-tg-backup process --config PATH` | 不执行发现，只处理队列 |
| `asmr-tg-backup status --config PATH` | 输出队列与最近项目状态 |
| `asmr-tg-backup enqueue URL --config PATH` | 手动加入一个 YouTube URL |
| `asmr-tg-backup sources path --config PATH` | 查看当前来源目录路径 |
| `asmr-tg-backup sources list --config PATH` | 查看过滤器和每个来源的完整配置字段 |
| `asmr-tg-backup sources validate [--file PATH] --config PATH` | 校验当前目录或另一文件，但不应用 |
| `asmr-tg-backup sources apply [--file PATH] --config PATH` | 同步当前目录，或从另一份有效文件原子替换后同步 |
| `asmr-tg-backup sources export --output PATH [--config PATH]` | 导出一份私有来源目录快照 |
| `asmr-tg-backup sources migrate --config PATH` | 从旧版 TOML/SQLite 创建统一目录 |
| `asmr-tg-backup extensions list [--config PATH]` | 不导入扩展代码，列出受信、已安装与已启用扩展 |
| `asmr-tg-backup extensions enable NAME [--config PATH]` | 安装、最小配置、校验、启用并安全重启一个受信扩展 |
| `asmr-tg-backup extensions doctor [--config PATH]` | 校验所有已启用扩展组成的运行时 |

### 引导式 setup 选项

| Setup 中显示的选项 | 结果 |
| --- | --- |
| MTProto 直接上传 | 官方安装可直接使用；源码 setup 会询问自己的 application ID/hash |
| 已有可信 API URL | 假定端点支持大文件，生成 1.99 GB 单文件限制并关闭分块；其他端点需修改生成的限制 |
| 本地 `telegram-bot-api` 用户服务 | 在 `127.0.0.1:18081` 注册预装的可执行文件 |
| `api.telegram.org` 音频分块 | 使用 49 MB 安全限制与可播放音频分块 |

## 原生路径

XDG 变量会替换对应的默认根目录。

| 资源 | XDG 路径 | 默认路径 |
| --- | --- | --- |
| Setup 配置 | `$XDG_CONFIG_HOME/asmr-tg-backup/config.toml` | `~/.config/asmr-tg-backup/config.toml` |
| 受管扩展状态 | 每份主配置旁的 `NAME.extensions.toml` | `~/.config/asmr-tg-backup/config.extensions.toml` |
| 私密扩展配置 | 主配置旁的 `extensions/` 目录 | `~/.config/asmr-tg-backup/extensions/` |
| 来源目录 | 默认与 setup 配置同目录 | `~/.config/asmr-tg-backup/sources.toml` |
| Worker 环境文件 | 与 setup 配置同目录的 `env` | `~/.config/asmr-tg-backup/env` |
| Worker unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup.service` | `~/.config/systemd/user/asmr-tg-backup.service` |
| 应用数据 | `$XDG_DATA_HOME/asmr-tg-backup` | `~/.local/share/asmr-tg-backup` |
| 数据库 | 应用数据目录下 | `~/.local/share/asmr-tg-backup/state.db` |
| 下载 | 应用数据目录下 | `~/.local/share/asmr-tg-backup/downloads` |
| MTProto session | 配置在应用数据目录下 | `~/.local/share/asmr-tg-backup/telegram-mtproto.session` |
| 本地 API env | `$XDG_CONFIG_HOME/asmr-tg-backup/telegram-bot-api.env` | `~/.config/asmr-tg-backup/telegram-bot-api.env` |
| 本地 API unit | `$XDG_CONFIG_HOME/systemd/user/asmr-tg-backup-telegram-bot-api.service` | `~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service` |
| 本地 API 数据 | `$XDG_DATA_HOME/asmr-tg-backup/telegram-bot-api` | `~/.local/share/asmr-tg-backup/telegram-bot-api` |

Docker 设置 `ASMR_TG_BACKUP_DATA_DIR=/data`，把宿主机可写目录 `./settings` 挂载到
`/settings`，并使用 `/settings/sources.toml`。必须挂载目录而不是只挂载单个文件，
否则来源目录无法完成原子替换。命名卷 `asmr-data` 保存数据库、下载与 MTProto session。

## 配置区块

| 区块 | 用途 |
| --- | --- |
| `[app]` | 数据路径、轮询、重试、租约、工作线程数量和日志 |
| `[sources]` | 指向统一的 `sources.toml` 来源目录；Panel 与 CLI 操作同一文件 |
| `[extensions]` | 已启用的扩展入口点 ID |
| `[extensions."id"]` | 必需标记、私密配置路径与行内扩展选项 |
| `[download]` | yt-dlp、ffmpeg、格式、路径、超时和附属元数据 |
| `[download.provider_profiles.*]` | 各来源类型的下载覆盖 |
| `[telegram]` | 启用、token、目标、transport、媒体和 caption |
| `[telegram.mtproto]` | Application 凭据对、session 路径与 MTProto 大小限制 |
| `[telegram.bot_api]` | Bot API 地址、大小限制与可播放分块 |
| `[control]` | Telegram 面板地址、权限与轮询 |
| `[twitch]` | Helix 凭据与 VOD/live 行为 |
| `[live]` | 与提供方无关的直播轮询、重试、worker 数量与录制超时 |

`config.toml` 是进程配置；具体来源和全局来源过滤器保存在 `sources.toml`，因此 Panel
修改来源时不会重写 `config.toml`。修改全局设置后重启进程；手工修改来源目录后依次
运行 `sources validate` 与 `sources apply`。

## 环境变量

| 变量 | 覆盖或控制 |
| --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | `[app].data_dir` |
| `ASMR_TG_BACKUP_SOURCES_PATH` | `[sources].path` |
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | `telegram.bot_api.api_base`；未设置 `control.api_base` 时由控制面继承 |
| `TELEGRAM_MAX_UPLOAD_BYTES` | `telegram.bot_api.max_upload_bytes` |
| `TWITCH_CLIENT_ID` | Twitch client ID |
| `TWITCH_ACCESS_TOKEN` | 已有 Twitch app access token |
| `TWITCH_CLIENT_SECRET` | Twitch app-token 创建与刷新 |

两个 MTProto 变量必须一起出现。官方安装包用户通常可以全部留空。源码构建选择
MTProto 时需要自己的完整凭据对；运行时凭据对会覆盖私有 TOML 中的凭据对。

## 投递流程

```text
发现 -> 入队 -> 下载 -> 准备媒体
  -> 所选 transport 准备/上传
  -> Telegram 接受消息或媒体组
  -> 保存 Telegram message ID
```

媒体根据 `upload_transport` 使用 MTProto 或 Bot API。只有选择 Bot API 且超过其字节
限制时，才执行音频分块。结果不明确的发送会进入 `uncertain`，不会换一种 transport
再次发送。

无论媒体使用哪种 transport，控制面板仍通过 Bot API 通信。

## 安全边界

- 配置、来源目录、环境文件、SQLite 和 `.session` 文件都应保持私密。
- 绝不要把 bot token 或 session 写入包、镜像、issue 或日志。
- MTProto application 凭据必须完整来自同一来源，不能混用两边。
- 经过不可信网络访问的远程 Bot API 地址使用 HTTPS；回环地址与受控私有 Compose
  网络可以使用 HTTP。
- 本地 Bot API 和统计端点只绑定可信接口。
- 媒体出口代理与回环 Telegram API 流量保持隔离。
- 只有具备同等凭据保护能力的备份位置才可以保存 session。
