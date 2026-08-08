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

## 缺少 `ffmpeg`、`ffprobe` 或 `curl`

PyPI 包不能安装操作系统二进制，请使用主机包管理器安装 `ffmpeg` 和 `curl`。
MTProto 媒体上传不使用 curl，但 Bot API transport 和 Telegram 控制面板需要它。

## 缺少 MTProto application 凭据

官方 PyPI 与 GHCR 发行包可以使用默认 application identity。源码构建需要自己的
完整凭据对：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

两个值必须来自同一个完整来源。只设置一个环境变量时，请删除它或补齐另一半。
环境变量覆盖私有 TOML，因此不能通过只填一个 TOML 字段来补全残缺环境变量。

还应确认服务管理器确实读取了环境文件：

```bash
systemctl --user show asmr-tg-backup.service -p EnvironmentFiles
```

## 无法创建或复用 MTProto session

确认 `telegram.mtproto.session_path` 最终位于可写、持久化的目录。Docker 中通常应在
`/data` 下；原生 setup 则放在 `~/.local/share/asmr-tg-backup/` 下。

停止重复的应用进程。两个进程不能共享同一个 Telethon SQLite session。不要为了重试
上传就删除健康 session；这会丢弃授权状态。

session 应按凭据处理。如果它可能被复制或泄露，请停止服务并替换授权，不要把文件
公开出来排错。

## MTProto 无法解析目标地址

有条件时优先使用 `@archive_channel` 形式的频道用户名。私有频道或数字 peer 需要确认
bot 已加入并具备发言权限，而且持久 session 可以解析该 peer。测试时只运行一个进程，
避免 entity/session 更新被分散到多个 client。

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
端点。document 和 video 不会使用可播放音频分块策略。

## 分段标题错误或没有封面

这一行为只属于 Bot API 分块路径。当前分段组使用 `Part i/n` 标题，并为每一项独立
上传 thumbnail。请确认 `media_type = "audio"`、检查准备出的封面，并确认升级后已经
重启运行服务。

## 应用无法访问 Bot API

- 原生本地服务：`http://127.0.0.1:18081`。
- Compose `local-api`：`http://telegram-bot-api:8081`。
- Docker 主机或远程服务：使用容器内可路由的地址。

不要使用应用容器内的 `127.0.0.1` 访问其他容器或 Docker 主机。检查是否有旧的
`TELEGRAM_API_BASE` 环境变量覆盖。回环请求会按设计绕过继承的代理。

## 本地 Bot API 拒绝 bot token

云端和本地 Bot API 有迁移要求。请执行 Telegram
[官方流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)
并等待完成。`asmr-tg-backup setup` 不会调用云端 `logOut`。

对于原生 setup，wheel 不会安装 `telegram-bot-api`；请先按照
[官方源码说明](https://github.com/tdlib/telegram-bot-api#installation)构建 C++ 服务端。

## 投递状态为 `uncertain`

Telegram 接受消息后仍可能发生超时或连接断开。服务会记录该状态，不会自动改用
MTProto、Bot API 或分块重试，因为再次发送可能产生重复消息。请先检查目标频道和
本地任务状态，再决定如何恢复。
