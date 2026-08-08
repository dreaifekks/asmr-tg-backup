# Telegram Bot API Local Server

Reusable Docker Compose service for a local Telegram Bot API endpoint. This
example uses the third-party, non-Telegram-maintained
`aiogram/telegram-bot-api` image.

For a new full-stack deployment, prefer the repository-root `compose.yaml`,
which builds `asmr-tg-backup` and enables this API with the `local-api` profile.
This directory remains available when one host-level Bot API is intentionally
shared by several native services.

It is intended to be shared by local services on the same host. The compose file
binds the API and stats ports to `127.0.0.1` only:

- Bot API: `http://127.0.0.1:18081`
- Stats: `http://127.0.0.1:8082`

Do not expose the stats port outside localhost; it can include bot details.

## Setup

Run these commands from the `asmr-tg-backup` repository root:

```bash
mkdir -p ~/services/telegram-bot-api
cp deploy/telegram-bot-api/compose.yaml ~/services/telegram-bot-api/
cp deploy/telegram-bot-api/.env.example ~/services/telegram-bot-api/.env
chmod 600 ~/services/telegram-bot-api/.env
```

Fill `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env` using values from
<https://my.telegram.org/apps>. These are Telegram application credentials, not
the BotFather bot token.

Then start the service:

```bash
cd ~/services/telegram-bot-api
docker compose up -d
docker compose ps
curl -sS http://127.0.0.1:8082
```

## Use From Bots

Point Bot API clients at:

```text
http://127.0.0.1:18081
```

For `asmr-tg-backup`, set:

```toml
[telegram]
upload_transport = "bot_api"

[telegram.bot_api]
api_base = "http://127.0.0.1:18081"
max_upload_bytes = 1990000000
split_large_audio = false
```

When a bot token is switched to a local Bot API server, Telegram may require
logging out from the cloud Bot API before the token can be used locally. If the
local server returns a conflict/login error, call:

```bash
printf 'url = "https://api.telegram.org/bot%s/logOut"\n' "$BOT_TOKEN" \
  | curl --silent --show-error --config -
```

This passes the token to `curl` through standard input instead of exposing it in
the `curl` process arguments. After a successful logout, the bot can use the
local server, but it cannot log back in to Telegram's cloud Bot API for 10
minutes.
