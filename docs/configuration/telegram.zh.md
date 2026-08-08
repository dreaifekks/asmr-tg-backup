# Telegram 投递

只有同时提供 bot token、目标地址并设置 `telegram.enabled = true`，投递才会启用。
本应用使用 Telegram API。

## 默认 MTProto 配置

```toml
[telegram]
enabled = true
bot_token = ""
chat_id = "@your_channel"
upload_transport = "mtproto"
media_type = "audio"
send_as_document = false
upload_timeout_seconds = 7200

[telegram.mtproto]
session_path = "telegram-mtproto.session"
max_upload_bytes = 1990000000

[telegram.bot_api]
api_base = "https://api.telegram.org"
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

相对的 `session_path` 会解析到 `[app].data_dir` 下面；Docker 中即 `/data`。
session 保存 bot 授权，必须保持私密。不要共享、写入镜像或提交到仓库。

官方 PyPI 与 GHCR 发行包可以使用默认 MTProto 路径。源码构建需要在私有配置中
提供自己的 application 凭据对：

```toml
[telegram.mtproto]
api_id = 123456
api_hash = "0123456789abcdef0123456789abcdef"
```

也可以在运行时成对提供：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

只接受完整的一对。运行时变量覆盖私有 TOML，私有 TOML 再覆盖官方发行包可用的
默认值。

## 环境变量覆盖

| 变量 | 用途 | TOML 回退 |
| --- | --- | --- |
| `ASMR_TG_BACKUP_DATA_DIR` | 数据库、下载和相对 session 的根目录 | `[app].data_dir` |
| `TELEGRAM_BOT_TOKEN` | BotFather token | `telegram.bot_token` |
| `TELEGRAM_CHAT_ID` | 目标聊天或频道 | `telegram.chat_id` |
| `ASMR_TG_UPLOAD_TRANSPORT` | `mtproto` 或 `bot_api` | `telegram.upload_transport` |
| `ASMR_TG_MTPROTO_API_ID` | MTProto application ID | `telegram.mtproto.api_id` |
| `ASMR_TG_MTPROTO_API_HASH` | MTProto application hash | `telegram.mtproto.api_hash` |
| `TELEGRAM_API_BASE` | Bot API 地址 | `telegram.bot_api.api_base` |
| `TELEGRAM_MAX_UPLOAD_BYTES` | Bot API 单文件安全限制 | `telegram.bot_api.max_upload_bytes` |

环境文件应保持 `0600`。MTProto application 凭据对并不是 bot token，也不能替代它。

## 选择 transport

`upload_transport` 决定媒体上传器：

- `mtproto` 通过一个持久化 MTProto client 上传，不需要额外 Bot API 服务；
- `bot_api` 使用配置的 HTTP Bot API 端点。

即使媒体使用 MTProto，Telegram 控制面板仍使用 Bot API 地址。如果启用了控制面板，
请确保 `[telegram.bot_api].api_base` 可访问。

服务不会在一次结果不明确的发送后改用另一种 transport 重试。Telegram 可能已经接受
消息时发生超时，会被记录为 uncertain，以避免重复消息。transport 切换必须显式配置。

## MTProto 媒体上传

默认 1,990,000,000 字节的应用限制为 Telegram 普通上传上限预留了余量。MTProto
直接上传媒体，保持正常的标题、caption 和封面行为，不创建音频分段。

大文件可以通过 `pip install "asmr-tg-backup[performance]"` 安装可选 `cryptg`
来加快加密；它不会改变投递语义。

上传器使用单一进程级 client。不要把应用横向扩容为多个共享同一 SQLite 状态或
MTProto session 的进程。

## 使用已有 Bot API

```toml
[telegram]
upload_transport = "bot_api"

[telegram.bot_api]
api_base = "https://api.telegram.org"
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

使用可信远程或本地服务时，请替换 `api_base`，并设置该端点实际支持的保守限制。
非回环 URL 应使用 HTTPS。Bot API 方法 URL 包含 token，因此不要让请求经过不可信
代理，也不要记录完整请求 URL。回环请求会显式绕过继承的 HTTP 代理设置。

## 可播放音频分块

分块是 Bot API 的超限策略，不是第三种 transport。当音频超过
`telegram.bot_api.max_upload_bytes` 且已启用分块时，ffmpeg 会创建 2-10 个可独立
播放的 M4A/MP3 分段，并作为一个媒体组发送。

每一段都会得到：

- 以 `Part i/n` 结尾的独立标题；
- 自己的 thumbnail attachment；
- 完整可播放的媒体容器。

caption 放在第一项。如果文件无法在 `max_upload_parts` 内装下，投递会给出明确的超限
原因并停止，不会无限重试。document 与 video 必须符合端点本身的限制。

## 本地 Bot API

原生 setup 可以把预装的 `telegram-bot-api` 注册成私有用户服务，监听
`127.0.0.1:18081`。Compose 则通过 `local-api` profile 和服务地址
`http://telegram-bot-api:8081` 提供相同的高级布局。

服务端的 `TELEGRAM_API_ID/HASH` 与应用自身的
`ASMR_TG_MTPROTO_API_ID/HASH` 是两套独立变量。setup 不会下载 C++ 服务端，也不会
自动执行 Telegram bot 迁移。把云端使用过的 token 转移到本地前，请执行
[官方迁移流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)。

## Caption 与媒体类型

`caption_template` 可以使用 `{title}`、`{url}`、`{feed_name}`、`{video_id}` 和
`{tag}`。`media_type` 接受 `audio`、`video` 或 `document`；
`send_as_document = true` 会强制使用 document 投递。

上传失败不会删除本地归档主文件。
