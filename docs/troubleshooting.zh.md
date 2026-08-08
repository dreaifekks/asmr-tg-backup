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

- 原生本地服务：`http://127.0.0.1:18081`。
- Compose `local-api`：`http://telegram-bot-api:8081`。
- Docker 主机或远程服务：使用容器内可路由的地址。

不要使用应用容器内的 `127.0.0.1` 访问其他容器或 Docker 主机。检查是否有旧的
`TELEGRAM_API_BASE` 环境变量覆盖。回环请求会按设计绕过继承的代理。上面的回环地址与
受控私有 Compose 地址可以使用普通 HTTP；经过不可信网络访问的远程端点应使用 HTTPS。

## 本地 Bot API 拒绝 bot token

云端和本地 Bot API 有迁移要求。请执行 Telegram
[官方流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" }
并等待完成。`asmr-tg-backup setup` 不会调用云端 `logOut`。

对于原生 setup，wheel 不会安装 `telegram-bot-api`；请先按照
[官方源码说明](https://github.com/tdlib/telegram-bot-api#installation){ target="_blank" rel="noopener noreferrer" }构建 C++ 服务端。

## 投递状态为 `uncertain`

Telegram 接受消息后仍可能发生超时或连接断开。服务会记录该状态，不会自动改用
MTProto、Bot API 或分块重试，因为再次发送可能产生重复消息。请先检查目标频道和
本地任务状态，再决定如何恢复。
