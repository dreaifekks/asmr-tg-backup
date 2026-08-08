# Docker Compose

Compose runs one application container with persistent state under `/data`.
MTProto is the default media transport; the `local-api` profile is optional and
intended only for advanced Bot API deployments.

## Prepare private files

```bash
cp .env.example .env
cp config.example.toml config.toml
chmod 600 .env config.toml
```

At minimum, edit `.env`:

```dotenv
ASMR_TG_BACKUP_IMAGE=ghcr.io/dreaifekks/asmr-tg-backup:latest
TELEGRAM_BOT_TOKEN=replace-with-the-bot-token
TELEGRAM_CHAT_ID=-1001234567890
ASMR_TG_UPLOAD_TRANSPORT=mtproto
```

Set `PUID` and `PGID` to the output of `id -u` and `id -g`. The container
matches its runtime account to those values before reading the mode-`0600`
config and then drops root privileges; `/data` is corrected only when its
volume ownership does not already match.

Set `telegram.enabled = true` in `config.toml` only when ready to deliver.

## Run the official image

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=100 asmr-tg-backup
```

The official GHCR image can use MTProto after the bot token and destination are
configured. The session is created below `/data` and therefore survives
container replacement in the `asmr-data` volume.

To use your own Telegram application identity, set both values:

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

Never copy a session into the image or publish the `/data` volume.

## Build the source checkout

The Compose build explicitly selects the Dockerfile's `source-runtime` target:

```dotenv
ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:local
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

```bash
docker compose build asmr-tg-backup
docker compose up -d asmr-tg-backup
```

A source-built image needs the complete application credential pair for
MTProto. The official release workflow uses the separately verified wheel for
the published GHCR image, so the official PyPI and container releases run the
same Python artifact.

## Use an existing Bot API

Select the Bot API transport and set a URL routable from the container:

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

The official endpoint uses playable splitting according to
`[telegram.bot_api]`. Replace the URL and limit for a trusted existing server.

!!! warning "Container loopback"

    `127.0.0.1` inside the application is the application container, not the
    Docker host. Use a service-network address, or choose native deployment for
    a host service bound only to loopback.

## Optional Compose-managed local Bot API

Set the server credentials and service-network URL:

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_MAX_UPLOAD_BYTES=1990000000
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

Then start the profile:

```bash
docker compose --profile local-api up -d
docker compose logs --tail=100 asmr-tg-backup telegram-bot-api
```

`TELEGRAM_API_ID/HASH` configure the Bot API server. They are separate from the
application's `ASMR_TG_MTPROTO_API_ID/HASH` variables.

Before moving a token already used on Telegram's cloud Bot API, follow the
[local-server migration procedure](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server).

## Update and verify

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

Back up both named volumes before an upgrade that changes storage. Do not scale
the application service: SQLite polling and Telegram updates assume one process.
