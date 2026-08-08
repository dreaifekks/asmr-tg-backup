# Docker Compose

Compose 运行一个应用容器，持久状态位于 `/data`。MTProto 是默认媒体 transport；
`local-api` profile 只是高级 Bot API 部署的可选项。

## 准备私密文件

```bash
cp .env.example .env
cp config.example.toml config.toml
chmod 600 .env config.toml
```

至少修改 `.env` 中这些值：

```dotenv
ASMR_TG_BACKUP_IMAGE=ghcr.io/dreaifekks/asmr-tg-backup:latest
TELEGRAM_BOT_TOKEN=replace-with-the-bot-token
TELEGRAM_CHAT_ID=-1001234567890
ASMR_TG_UPLOAD_TRANSPORT=mtproto
```

请把 `PUID`、`PGID` 设置为 `id -u`、`id -g` 的输出。容器会先把运行账户匹配到
这两个值，以便读取 mode-`0600` 的配置，然后降权运行；只有 `/data` 数据卷归属不匹配
时才会修正其权限。

只有准备开始投递时，才在 `config.toml` 中设置 `telegram.enabled = true`。

## 运行官方镜像

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=100 asmr-tg-backup
```

提供 bot token 与目标地址后，官方 GHCR 镜像可以使用 MTProto。session 会在
`/data` 下创建，并通过 `asmr-data` 卷在容器替换后继续保留。

需要使用自己的 Telegram application identity 时，请成对设置：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

不要把 session 复制进镜像，也不要公开 `/data` 卷。

## 构建源码 checkout

Compose 构建会明确选择 Dockerfile 的 `source-runtime` target：

```dotenv
ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:local
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

```bash
docker compose build asmr-tg-backup
docker compose up -d asmr-tg-backup
```

源码镜像使用 MTProto 时需要完整的 application 凭据对。官方发布流则使用已经验证的
wheel 构建 GHCR 镜像，因此官方 PyPI 与容器发行包运行的是同一个 Python 制品。

## 使用已有 Bot API

选择 Bot API transport，并填写容器内可路由的 URL：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

官方端点按照 `[telegram.bot_api]` 使用可播放分块。使用已有可信服务器时，请替换
URL 与限制。

!!! warning "容器回环地址"

    应用容器中的 `127.0.0.1` 指向应用容器自身，不是 Docker 主机。请使用服务网络
    地址；如果主机服务只绑定回环地址，则应选择原生部署。

## 可选的 Compose 本地 Bot API

设置服务端凭据与服务网络 URL：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_MAX_UPLOAD_BYTES=1990000000
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

然后启动 profile：

```bash
docker compose --profile local-api up -d
docker compose logs --tail=100 asmr-tg-backup telegram-bot-api
```

`TELEGRAM_API_ID/HASH` 用于配置 Bot API 服务端，与应用自身的
`ASMR_TG_MTPROTO_API_ID/HASH` 是两套独立变量。

把已经在 Telegram 云端 Bot API 使用的 token 迁到本地前，请执行
[本地服务迁移流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)。

## 更新与验证

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

涉及存储变更的升级前，请备份两个命名卷。不要横向扩容应用服务：SQLite 轮询和
Telegram updates 都按单一进程设计。
