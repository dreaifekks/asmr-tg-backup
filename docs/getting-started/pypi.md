# PyPI and native Linux

The PyPI package is the smallest deployment: one application process, SQLite,
and a private MTProto session. A local Bot API server is not required for the
default path.

## Install system tools

On Debian or Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg curl pipx
pipx ensurepath
```

Install the official release:

```bash
pipx install asmr-tg-backup
asmr-tg-backup --version
```

For faster encryption during large MTProto uploads, choose the optional-extra
form instead of the plain install command above:

```bash
pipx install "asmr-tg-backup[performance]"
```

`cryptg` is only a performance accelerator; the core package works without it.

## Run the guided setup

```bash
asmr-tg-backup setup
```

The default choice is **MTProto direct upload**. In an official release, setup
asks for:

1. the BotFather token;
2. the destination chat ID or `@channel`;
3. the Telegram user ID allowed to open the control panel.

It writes a mode-`0600` configuration below
`~/.config/asmr-tg-backup/`, initializes SQLite below
`~/.local/share/asmr-tg-backup/`, and prints the exact run command. It does not
send a test message. The first run creates the MTProto session in the data
directory; keep that session as private as the bot token.

## Source builds and your own Telegram application

Source builds need their own Telegram application ID/hash for MTProto. Obtain a
pair through Telegram's
[application setup](https://core.telegram.org/api/obtaining_api_id), then export
both values before setup and every service run:

```bash
export ASMR_TG_MTPROTO_API_ID=123456
export ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
asmr-tg-backup setup
```

The pair is atomic: configuring only one value is an error. You may instead
store both values in the private `[telegram.mtproto]` section. Never copy a bot
token or a `.session` file into a source tree.

## Run and verify

Use the path printed by setup, for example:

```bash
asmr-tg-backup run \
  --config ~/.config/asmr-tg-backup/config.toml
```

In another terminal:

```bash
asmr-tg-backup status \
  --config ~/.config/asmr-tg-backup/config.toml
```

Then send `/panel` to the bot and add one source. Verify one download and one
delivery before adding more sources.

## Run as a user service

Create a private environment file if the process needs source-build credentials
or Twitch credentials:

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

Keep it at `~/.config/asmr-tg-backup/env` with mode `0600`, and point a
`systemd --user` unit at both that file and the setup-generated configuration.
The `ExecStart` command should use the absolute executable path reported by
`command -v asmr-tg-backup`.

```ini
[Unit]
Description=ASMR archive and Telegram delivery worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/asmr-tg-backup run --config %h/.config/asmr-tg-backup/config.toml
EnvironmentFile=-%h/.config/asmr-tg-backup/env
Restart=always
RestartSec=10s
UMask=0077

[Install]
WantedBy=default.target
```

Save it as `~/.config/systemd/user/asmr-tg-backup.service`, adjusting
`ExecStart` if `command -v` returns a different path.

```bash
systemctl --user daemon-reload
systemctl --user enable --now asmr-tg-backup.service
journalctl --user -u asmr-tg-backup.service -f
```

## Advanced Bot API setup

Choose **Bot API** instead of MTProto in the first setup menu. The next menu can:

- use an existing trusted URL;
- validate a preinstalled `telegram-bot-api` executable and register a local
  user unit at `127.0.0.1:18081`;
- use `api.telegram.org` with a 49 MB safety limit and playable audio splitting.

The wheel never downloads the C++ server. Build it first using Telegram's
[official source instructions](https://github.com/tdlib/telegram-bot-api#installation)
when choosing the local-service branch. The local server needs its own
`TELEGRAM_API_ID`/`TELEGRAM_API_HASH`; these are separate configuration from the
application's `ASMR_TG_MTPROTO_API_ID/HASH` pair.

Setup never migrates a bot away from the cloud Bot API. Follow Telegram's
[local-server migration procedure](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)
before first use of a token on a local server.

## Update

```bash
pipx upgrade asmr-tg-backup
systemctl --user restart asmr-tg-backup.service
```

Back up the configuration, SQLite database, downloaded files, and MTProto
session before updating. See [Operate](../operations.md).
