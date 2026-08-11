# 故障排查

## 先查看状态与日志

=== "Docker Compose"

    ```bash
    docker compose ps
    docker compose logs --tail=200 asmr-tg-backup
    docker compose logs --tail=100 telegram-bot-api
    ```

    最后一条命令只在运行 `local-api` profile 时需要。

=== "原生安装"

    ```bash
    asmr-tg-backup status --config ~/.config/asmr-tg-backup/config.toml
    systemctl --user status asmr-tg-backup.service
    journalctl --user -u asmr-tg-backup.service --no-pager -n 200
    ```

## 来源修改没有显示

先查看当前进程实际使用的目录路径和解析结果：

```bash
asmr-tg-backup sources path --config /path/to/config.toml
asmr-tg-backup sources list --config /path/to/config.toml
```

Panel 对来源或过滤器的修改会写入这份 `sources.toml` 并立即同步 SQLite。手工编辑文件
后，需要依次成功执行：

```bash
asmr-tg-backup sources validate --config /path/to/config.toml
asmr-tg-backup sources apply --config /path/to/config.toml
```

不要直接编辑 `state.db` 来配置来源。Compose 应把可写的 `./settings` 目录挂载到
`/settings`，而不是只绑定 `sources.toml` 单个文件；原子替换需要目录可写。

## 缺少 `ffmpeg`、`ffprobe` 或 `curl`

PyPI 包不能安装操作系统二进制。需要媒体处理能力时，请使用主机包管理器安装
`ffmpeg` 和 `ffprobe`。只有使用 `upload_transport = "bot_api"` 投递媒体时才需要
安装 `curl`；MTProto 媒体上传和 Telegram 控制面板都不使用 `curl`。

## 缺少 MTProto application 凭据

官方 PyPI 与 GHCR 安装通常不需要配置 application 凭据。源码构建需要自己的完整
凭据对：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

API ID 和 hash 必须成对设置。环境变量优先于 TOML，因此请在同一位置填写完整的两项。

还应确认服务管理器确实读取了环境文件：

```bash
systemctl --user show asmr-tg-backup.service -p EnvironmentFiles
```

## 无法创建或复用 MTProto session

确认 `telegram.mtproto.session_path` 最终位于可写、持久化的目录。Docker 中通常应在
`/data` 下；原生 setup 则放在 `~/.local/share/asmr-tg-backup/` 下。

确保同一 session 只由一个应用进程使用。不要为了重试而删除仍可使用的 session。
session 包含 bot 授权信息，如有泄露请停止服务并重新授权。

## MTProto 无法解析目标地址

公开频道优先使用 `@archive_channel` 形式的用户名；私有频道使用数字 chat ID。确认 bot
已加入并具备发言权限，测试时只保留一个应用进程。

## 上传因文件过大被拒绝

检查选中的 transport 及其对应限制：

```toml
[telegram]
upload_transport = "mtproto"

[telegram.mtproto]
max_upload_bytes = 1990000000
```

Bot API 则检查 `[telegram.bot_api].max_upload_bytes`。官方端点应使用 49 MB 安全值
并启用可播放分块：

```toml
[telegram]
upload_transport = "bot_api"

[telegram.bot_api]
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

如果音频仍无法装入允许的分段数，请选择更小的音频格式，或改用 MTProto/可信本地
端点。document 和 video 不会使用可播放音频分块策略。transport 切换是手动配置
决定；服务不会自动回退。

## 分段标题错误或没有封面

这一行为只属于 Bot API 分块路径。当前分段组使用 `Part i/n` 标题，并为每一项独立
上传 thumbnail。请确认 `media_type = "audio"`、检查准备出的封面，并确认升级后已经
重启运行服务。

## 应用无法访问 Bot API

先确认失败的是哪条路径，因为媒体和 Panel 可以使用不同端点：

- 当 `telegram.upload_transport = "bot_api"` 时，Bot API 媒体上传使用
  `telegram.bot_api.api_base`；`TELEGRAM_API_BASE` 会覆盖这个值。
- `/panel`、`getUpdates`、callback、命令和 Panel 消息编辑优先使用非空的
  `control.api_base`；该值留空时继承上面的 Telegram Bot API 地址。
- MTProto 媒体投递不经过这两个 HTTP 端点；这种配置下只有 Panel 仍需要 Bot API。

选择应用进程能够访问的地址：

- 原生本地服务：`http://127.0.0.1:18081`。
- Compose `local-api`：`http://telegram-bot-api:8081`。
- Docker 主机或远程服务：使用容器内可路由的地址。

例如下面的配置会让媒体继续使用 MTProto，同时让 Compose Panel 连接本地 Bot API：

```toml
[telegram]
upload_transport = "mtproto"

[control]
api_base = "http://telegram-bot-api:8081"
```

应用容器内的 `127.0.0.1` 只指向应用容器本身；访问其他容器或 Docker 主机时，请使用
容器内可路由的地址。
媒体端点与预期不同时检查 `TELEGRAM_API_BASE`，它优先于
`[telegram.bot_api].api_base`；只有 Panel 端点异常时，先检查 `[control].api_base`。
回环请求会绕过继承的代理。上面的回环地址与受控私有 Compose 地址可以使用普通 HTTP；
经过不可信网络访问远程端点时使用 HTTPS。

## 本地 Bot API 拒绝 bot token

先停止应用，以及使用这个 token 的其他所有 `getUpdates` consumer。如果 token 已经
连接过 Telegram 云端 Bot API，请按照 Telegram
[官方流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" }
完成云端 `logOut`，并等待迁移完成。先启动本地服务，再启动应用；token 交给本地服务
期间，云端 poller 保持停止。`asmr-tg-backup setup` 会完成应用和本地服务配置；token
迁移是同一启用流程中需要手动完成的一步。

对于原生 setup，wheel 不会安装 `telegram-bot-api`；请先按照
[官方源码说明](https://github.com/tdlib/telegram-bot-api#installation){ target="_blank" rel="noopener noreferrer" }构建 C++ 服务端。

## 投递状态为 `uncertain`

Telegram 接受消息后仍可能发生超时或连接断开。服务会记录该状态，不会自动改用
MTProto、Bot API 或分块重试，因为再次发送可能产生重复消息。请先检查目标频道和
本地任务状态，再决定如何恢复。
