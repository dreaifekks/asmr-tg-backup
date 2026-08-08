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

## Use another Bot API endpoint

Set the transport and endpoint in `.env`:

```dotenv
ASMR_TG_UPLOAD_TRANSPORT=bot_api
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_MAX_UPLOAD_BYTES=49000000
```

The values in `[telegram.bot_api]` control playable audio splitting. Replace the
URL and size limit when connecting to another Bot API server.

## Run the local Bot API profile

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

Start both services:

```bash
docker compose --profile local-api up -d
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

For the official image:

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

For a source build:

```bash
docker compose build --pull asmr-tg-backup
docker compose up -d asmr-tg-backup
```

Back up `./settings/sources.toml` and `asmr-data` before updating. If the
`local-api` profile is enabled, also back up `telegram-bot-api-data`.
