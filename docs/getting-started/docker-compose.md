# Docker Compose

Compose runs the application with persistent state in the `asmr-data` volume.
MTProto is the default upload method. Enable the `local-api` profile when you
also want Compose to run a local Bot API server.

## 1. Get the deployment files

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

## 2. Configure the service

Print the host account IDs:

```bash
id -u
id -g
```

Put those values and the Telegram settings into `.env`:

```dotenv
PUID=1000
PGID=1000
ASMR_TG_BACKUP_IMAGE=ghcr.io/dreaifekks/asmr-tg-backup:latest
TELEGRAM_BOT_TOKEN=replace-with-the-bot-token
TELEGRAM_CHAT_ID=-1001234567890
ASMR_TG_UPLOAD_TRANSPORT=mtproto
```

Use the [Telegram shortcuts](index.md#telegram-shortcuts) to create the bot and
find your control-panel user ID.

To add Twitch sources, also put the application credentials in `.env`:

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

The [Twitch setup guide](../configuration/sources.md#twitch-credentials) links
to the developer console and explains the access-token alternative.

In the existing `[telegram]` and `[control]` sections of `config.toml`, change:

```toml
[telegram]
enabled = true

[control]
enabled = true
allowed_user_ids = ["123456789"]
```

The example YouTube and Twitch sources remain disabled. Add the first source
from `/panel` after the container starts. The panel updates the host file
`./settings/sources.toml`; `/data/state.db` only holds its synchronized mirror
and runtime state. Compose mounts the whole writable `./settings` directory so
the panel can replace the catalog atomically.

## 3. Start

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=100 asmr-tg-backup
```

Send `/panel` to the bot and add one YouTube or Twitch source. The first actual
MTProto delivery creates the bot session under `/data`; the `asmr-data` volume
keeps it across container updates.

## Install optional extensions {#install-extensions}

The official image contains the core application. A repeatable Docker extension
installation has three parts:

1. build a derived image that installs each selected extension package;
2. enable the matching runtime ID in the host `config.toml`;
3. mount a private extension file when that extension needs one.

Do not install an extension with `pip` inside a running container: that change
disappears when Compose replaces the container. Use the complete
[Docker extension installation](../configuration/extensions.md#docker-extensions)
workflow to create `Dockerfile.extensions`, build a versioned image, select it
through `ASMR_TG_BACKUP_IMAGE`, validate it, and start it with `--no-build`.

For the two reviewed extensions, the required pieces are:

| Extension | Package in derived image | ID in `config.toml` | Extra mount |
| --- | --- | --- | --- |
| Niconico source | `asmr-tg-backup-ext-niconico-origin==0.2.0` | `dreaife.niconico-origin` | None |
| Proxy routing | `asmr-tg-backup-ext-proxy-router==0.2.0` | `dreaife.proxy-router` | `./extensions:/config/extensions:ro` |

After building and configuring the image, run the checks before starting the
long-running service:

```bash
docker compose config --images
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
docker compose up -d --no-build asmr-tg-backup
```

When Niconico is enabled, open a new `/panel` or refresh the active panel after
startup. The panel then includes `➕ Niconico`.

## Choose which Bot API traffic uses another endpoint

First decide whether the endpoint will carry media uploads, panel traffic, or
both. `TELEGRAM_API_BASE` configures `telegram.bot_api.api_base`; media uses it
only when `ASMR_TG_UPLOAD_TRANSPORT=bot_api`. The panel uses
`control.api_base` when that field is set and otherwise inherits the Telegram
Bot API endpoint.

To send media through another Bot API endpoint, set the transport and endpoint
in `.env`:

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

The values in `[telegram.bot_api]` control media delivery and playable audio
splitting. Match the URL and size limit to the selected server.

To keep MTProto media delivery while moving only `/panel`, callbacks, and bot
commands to another endpoint, leave `ASMR_TG_UPLOAD_TRANSPORT=mtproto` and set
the control endpoint in `config.toml`:

```toml
[control]
enabled = true
api_base = "http://telegram-bot-api:8081"
```

Inside Compose, use the service URL above rather than `127.0.0.1`; loopback in
the application container points back to that container.

## Run the local Bot API profile

Use this profile when Compose should own the local Bot API server. Before
starting it, prepare the server credentials and complete the bot migration.

Create an API ID/hash from
[Telegram API development tools](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" },
then add these values to `.env`:

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_MAX_UPLOAD_BYTES=1990000000
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_BOT_API_IMAGE=aiogram/telegram-bot-api:latest
```

The example routes both media and panel traffic to the local server because
`control.api_base` inherits `TELEGRAM_API_BASE` when it is empty. To keep media
on MTProto and move only the control panel, set
`ASMR_TG_UPLOAD_TRANSPORT=mtproto` and use the `control.api_base` example above.

If this bot token has already connected to Telegram's cloud Bot API, move it in
this order:

1. Stop the application and every other `getUpdates` consumer for this token:

    ```bash
    docker compose stop asmr-tg-backup
    ```

2. Follow Telegram's
   [official local-server migration procedure](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" },
   including the cloud `logOut` call, and wait for it to complete.
3. Start the local Bot API first, then start the application. Keep cloud Bot API
   pollers stopped while this token is assigned to the local server:

```bash
docker compose --profile local-api up -d telegram-bot-api
docker compose up -d asmr-tg-backup
docker compose logs --tail=100 asmr-tg-backup telegram-bot-api
```

The profile uses the `aiogram/telegram-bot-api` image by default; change
`TELEGRAM_BOT_API_IMAGE` to use another image. Inside Compose, the application
reaches this service at `http://telegram-bot-api:8081`.

## Build from the source checkout

Set `ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:local` in `.env`, then configure both
`ASMR_TG_MTPROTO_API_ID` and `ASMR_TG_MTPROTO_API_HASH` before running:

```bash
docker compose build --pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

See [Architecture and development](../development.md) for the source workflow.

## Update

Before an update, stop the application and back up `.env`, `config.toml`,
`./settings/`, and `asmr-data`. Include any host-mounted private extension
configuration and Compose override, along with the Dockerfile or pinned package
list used to build a derived image. If the `local-api` profile is enabled, stop
that service too and back up `telegram-bot-api-data` while it is stopped.

```bash
docker compose stop asmr-tg-backup
# Run this line when the local-api profile is part of the deployment.
docker compose --profile local-api stop telegram-bot-api
```

For the official image:

```bash
docker compose pull asmr-tg-backup
```

For a source build:

```bash
docker compose build --pull asmr-tg-backup
```

An extension-enabled container uses the
[derived-image workflow](../configuration/extensions.md#docker-extensions).
Update the core tag in its `FROM` line, keep every extension version pinned,
rebuild that image under a new tag, and point `ASMR_TG_BACKUP_IMAGE` at the new
tag.

After preparing any of the three image types, validate the installed extensions
and source catalog from that image:

```bash
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

Then recreate only the application, or restore the complete local Bot API stack
when that profile is part of the deployment:

```bash
# Application only. Keep the already validated derived image:
docker compose up -d --no-build asmr-tg-backup

# Application plus the local Bot API profile:
docker compose --profile local-api up -d
```
