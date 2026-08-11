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

## 安装可选扩展 {#install-extensions}

官方镜像只包含核心应用。一套可以重复部署的 Docker 扩展安装包含三部分：

1. 构建派生镜像，把选中的扩展包装入镜像；
2. 在宿主机 `config.toml` 中启用对应的运行时 ID；
3. 扩展需要私密设置时，再挂载它的私密文件。

不要在运行中的容器里临时执行 `pip install`；Compose 替换容器后这次改动就会消失。
按照完整的 [Docker 扩展安装流程](../configuration/extensions.md#docker-extensions)
创建 `Dockerfile.extensions`、构建带版本的镜像、通过
`ASMR_TG_BACKUP_IMAGE` 选择它、完成校验，再用 `--no-build` 启动。

两个已审查扩展所需的内容如下：

| 扩展 | 派生镜像中的包 | `config.toml` 中的 ID | 额外挂载 |
| --- | --- | --- | --- |
| Niconico 来源 | `asmr-tg-backup-ext-niconico-origin==0.2.0` | `dreaife.niconico-origin` | 无 |
| 代理选路 | `asmr-tg-backup-ext-proxy-router==0.2.0` | `dreaife.proxy-router` | `./extensions:/config/extensions:ro` |

镜像构建和配置完成后，先执行检查，再启动长期运行的服务：

```bash
docker compose config --images
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
docker compose up -d --no-build asmr-tg-backup
```

启用 Niconico 后，启动完成再发送新的 `/panel` 或刷新当前有效的 Panel，即可看到
`➕ Niconico`。

## 选择哪些 Bot API 流量使用其他地址

先确定新地址承载媒体上传、Panel 流量，还是两者都承载。`TELEGRAM_API_BASE` 对应
`telegram.bot_api.api_base`；只有 `ASMR_TG_UPLOAD_TRANSPORT=bot_api` 时，媒体才会使用
这个地址。Panel 会优先使用 `control.api_base`，留空时再继承 Telegram Bot API 地址。

需要通过其他 Bot API 地址上传媒体时，在 `.env` 中切换 transport 并填写地址：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

`[telegram.bot_api]` 中的设置控制媒体投递与可播放音频分块。请按照目标服务填写 URL
和单文件大小。

如果媒体继续使用 MTProto，只把 `/panel`、callback 和 bot 命令切到其他地址，请保留
`ASMR_TG_UPLOAD_TRANSPORT=mtproto`，并在 `config.toml` 中设置控制端点：

```toml
[control]
enabled = true
api_base = "http://telegram-bot-api:8081"
```

在 Compose 网络内填写上面的服务名地址；应用容器里的 `127.0.0.1` 只指向应用容器
本身。

## 启动本地 Bot API profile

需要由 Compose 运行本地 Bot API 服务时使用这个 profile。启动前，先准备服务凭据并
完成 bot 迁移。

在 [Telegram API development tools](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" }
创建 API ID/hash，再把下面的值写入 `.env`：

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_MAX_UPLOAD_BYTES=1990000000
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_BOT_API_IMAGE=aiogram/telegram-bot-api:latest
```

示例会让媒体和 Panel 都连接本地服务，因为 `control.api_base` 留空时会继承
`TELEGRAM_API_BASE`。如果媒体继续使用 MTProto、只有控制面使用本地服务，请把
`ASMR_TG_UPLOAD_TRANSPORT` 设为 `mtproto`，并采用上面的 `control.api_base` 配置。

如果这个 bot token 已经连接过 Telegram 云端 Bot API，请按下面顺序迁移：

1. 停止应用，以及使用这个 token 的其他所有 `getUpdates` consumer：

    ```bash
    docker compose stop asmr-tg-backup
    ```

2. 按照 Telegram 的
   [本地服务迁移流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" }
   完成云端 `logOut`，并等待迁移完成。
3. 先启动本地 Bot API，再启动应用。这个 token 交给本地服务期间，云端 Bot API poller
   保持停止：

```bash
docker compose --profile local-api up -d telegram-bot-api
docker compose up -d asmr-tg-backup
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

更新前先停止应用，并备份 `.env`、`config.toml`、`./settings/` 和 `asmr-data`。如果
挂载了扩展私密配置，也把对应的宿主机文件和 Compose override、派生镜像使用的
Dockerfile 或固定版本清单纳入备份。启用了 `local-api` profile 时，同时停止该服务，
并在停止状态下备份 `telegram-bot-api-data`。

```bash
docker compose stop asmr-tg-backup
# 部署包含 local-api profile 时，再运行这一行。
docker compose --profile local-api stop telegram-bot-api
```

使用官方镜像：

```bash
docker compose pull asmr-tg-backup
```

使用源码构建：

```bash
docker compose build --pull asmr-tg-backup
```

启用扩展的容器使用[派生镜像流程](../configuration/extensions.md#docker-extensions)。
更新 `FROM` 中的核心版本，继续固定每个扩展的精确版本，用新标签重新构建镜像，再让
`ASMR_TG_BACKUP_IMAGE` 指向新标签。

准备好上述任一种镜像后，用该镜像校验扩展和来源目录：

```bash
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

随后只重建应用，或者在部署包含本地 Bot API 时恢复整个 profile：

```bash
# 仅应用；继续使用刚刚校验过的派生镜像：
docker compose up -d --no-build asmr-tg-backup

# 应用与本地 Bot API profile：
docker compose --profile local-api up -d
```
