# Docker Compose

Compose 通过 `asmr-data` 卷保存应用数据，默认使用 MTProto 上传。如需同时运行本地
Bot API，可启用 `local-api` profile。

## 1. 获取部署文件

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
cp .env.example .env
cp config.example.toml config.toml
mkdir -p settings
cp sources.example.toml settings/sources.toml
chmod 700 settings
chmod 600 .env config.toml settings/sources.toml
```

## 2. 填写配置

先查看当前账户的 UID 和 GID：

```bash
id -u
id -g
```

把结果和 Telegram 配置写入 `.env`：

```dotenv
PUID=1000
PGID=1000
ASMR_TG_BACKUP_IMAGE=ghcr.io/dreaifekks/asmr-tg-backup:latest
TELEGRAM_BOT_TOKEN=replace-with-the-bot-token
TELEGRAM_CHAT_ID=-1001234567890
ASMR_TG_UPLOAD_TRANSPORT=mtproto
```

创建 bot 和查看控制面板用户 ID，可以直接使用
[Telegram 快捷入口](index.md#telegram-shortcuts)。

准备添加 Twitch 来源时，还需要把 Twitch 应用凭据写入 `.env`：

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

[Twitch 配置说明](../configuration/sources.md#twitch-credentials)提供开发者控制台入口，
也说明了已有 access token 的用法。

在 `config.toml` 已有的 `[telegram]` 和 `[control]` 区块中修改：

```toml
[telegram]
enabled = true

[control]
enabled = true
allowed_user_ids = ["123456789"]
```

模板中的 YouTube 和 Twitch 来源默认关闭。容器启动后，从 `/panel` 添加第一个来源。
Panel 会更新宿主机的 `./settings/sources.toml`；`/data/state.db` 只保存同步镜像和运行
状态。Compose 挂载整个可写的 `./settings` 目录，使 Panel 可以原子替换目录文件。

## 3. 启动

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=100 asmr-tg-backup
```

向 bot 发送 `/panel`，添加一个 YouTube 或 Twitch 来源。首次通过 MTProto 投递时会在
`/data` 下创建 bot session；更新容器时，`asmr-data` 卷会继续保留它。

## 使用其他 Bot API 地址

在 `.env` 中切换 transport 并填写地址：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

`[telegram.bot_api]` 中的设置控制可播放音频分块。连接其他 Bot API 服务时，替换 URL
和单文件大小即可。

## 启动本地 Bot API profile

先在 [Telegram API development tools](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" }
创建 API ID/hash，再把下面的值写入 `.env`：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_MAX_UPLOAD_BYTES=1990000000
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_BOT_API_IMAGE=aiogram/telegram-bot-api:latest
```

启动两个服务：

```bash
docker compose --profile local-api up -d
docker compose logs --tail=100 asmr-tg-backup telegram-bot-api
```

这个 profile 默认使用 `aiogram/telegram-bot-api` 镜像；如需更换镜像，修改
`TELEGRAM_BOT_API_IMAGE`。在 Compose 网络内，应用通过
`http://telegram-bot-api:8081` 连接它。

## 从源码构建镜像

在 `.env` 中设置 `ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:local`，并分别填写
`ASMR_TG_MTPROTO_API_ID` 和 `ASMR_TG_MTPROTO_API_HASH`，然后运行：

```bash
docker compose build --pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

源码开发流程见[架构与开发](../development.md)。

## 更新

使用官方镜像：

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

使用源码构建：

```bash
docker compose build --pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

更新前备份 `./settings/sources.toml` 和 `asmr-data`。启用了 `local-api` profile 时，
再备份 `telegram-bot-api-data`。
