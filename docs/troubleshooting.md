# Troubleshooting

## Start with status and logs

=== "Docker Compose"

    ```bash
    docker compose ps
    docker compose logs --tail=200 asmr-tg-backup
    docker compose logs --tail=100 telegram-bot-api
    ```

    The last command matters only when the `local-api` profile is running.

=== "Native"

    ```bash
    asmr-tg-backup status --config ~/.config/asmr-tg-backup/config.toml
    systemctl --user status asmr-tg-backup.service
    journalctl --user -u asmr-tg-backup.service --no-pager -n 200
    ```

## A source change is not visible

First print the catalog path used by this process and inspect its parsed values:

```bash
asmr-tg-backup sources path --config /path/to/config.toml
asmr-tg-backup sources list --config /path/to/config.toml
```

Panel source/filter changes write that `sources.toml` and synchronize SQLite
immediately. A manual file edit is not applied until both commands succeed:

```bash
asmr-tg-backup sources validate --config /path/to/config.toml
asmr-tg-backup sources apply --config /path/to/config.toml
```

Do not edit `state.db` to configure a source. In Compose, confirm that the
container mounts the writable `./settings` directory at `/settings`, rather
than bind-mounting only `sources.toml`; atomic replacement needs the directory.

## `ffmpeg`, `ffprobe`, or `curl` is missing

The PyPI package cannot install operating-system binaries. Install `ffmpeg` and
`ffprobe` with the host package manager when media processing needs them.
Install `curl` only when `upload_transport = "bot_api"` is used for media
delivery. MTProto media upload and the Telegram control panel do not use `curl`.

## MTProto application credentials are missing

Official PyPI and GHCR installations normally need no application-credential
configuration. A source build needs its own complete pair:

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

Both values must be present in the same source. If only one environment value
is set, remove it or supply its partner. Environment values override private
TOML, so an incomplete environment pair cannot be repaired by one TOML field.

Also confirm the service manager actually loaded the environment file:

```bash
systemctl --user show asmr-tg-backup.service -p EnvironmentFiles
```

## MTProto session cannot be created or reused

Check that `telegram.mtproto.session_path` resolves inside a writable, persistent
directory. In Docker it should normally be below `/data`; on native setup it is
below `~/.local/share/asmr-tg-backup/`.

Stop duplicate application processes. Two processes must not share one Telethon
SQLite session. Do not delete a healthy session merely to retry an upload; doing
so discards authorization state.

Treat the session as a credential. If it may have been copied or exposed, stop
the service and replace the authorization rather than publishing the file for
debugging.

## MTProto cannot resolve the destination

Prefer a channel username such as `@archive_channel` when available. For a
private channel or numeric peer, ensure the bot is a member with permission to
post and that the persistent session can resolve that peer. Run only one process
while testing so entity/session updates are not split across clients.

## Upload is rejected as too large

Check the selected transport and its own limit:

```toml
[telegram]
upload_transport = "mtproto"

[telegram.mtproto]
max_upload_bytes = 1990000000
```

For Bot API, check `[telegram.bot_api].max_upload_bytes`. The official endpoint
should use the 49 MB safety value with playable splitting enabled:

```toml
[telegram]
upload_transport = "bot_api"

[telegram.bot_api]
max_upload_bytes = 49000000
split_large_audio = true
max_upload_parts = 10
```

If audio still cannot fit within the allowed part count, select a smaller audio
format or use MTProto/a trusted local endpoint. Document and video modes are not
split by the playable-audio policy. Changing transport is a manual configuration
decision; the service does not fall back automatically.

## Split items have the wrong title or no cover

This behavior belongs only to the Bot API splitting path. A current split group
uses `Part i/n` titles and uploads the thumbnail independently for every item.
Confirm `media_type = "audio"`, inspect the prepared thumbnail, and ensure the
running service was restarted after the update.

## The application cannot reach a Bot API

Identify the failing path first, because media and panel traffic can use
different endpoints:

- Bot API media uploads use `telegram.bot_api.api_base` when
  `telegram.upload_transport = "bot_api"`. `TELEGRAM_API_BASE` overrides this
  value.
- `/panel`, `getUpdates`, callbacks, commands, and panel message edits use
  `control.api_base` when it is non-empty. An empty value inherits the Telegram
  Bot API endpoint above.
- MTProto media delivery does not use either HTTP endpoint; only the panel still
  needs a Bot API endpoint in that configuration.

Use an address reachable from the application process:

- Native local service: `http://127.0.0.1:18081`.
- Compose `local-api`: `http://telegram-bot-api:8081`.
- Docker host or remote service: use an address routable from the container.

For example, this keeps media on MTProto while the Compose panel uses the local
Bot API service:

```toml
[telegram]
upload_transport = "mtproto"

[control]
api_base = "http://telegram-bot-api:8081"
```

Application-container `127.0.0.1` reaches that container itself, not another
container or the Docker host. If the media endpoint is unexpected, inspect
`TELEGRAM_API_BASE`; it takes precedence over `[telegram.bot_api].api_base`.
If only the panel is unexpected, inspect `[control].api_base` first. Loopback
requests bypass inherited proxies. Plain HTTP is suitable for the loopback and
controlled private Compose addresses above; use HTTPS for a remote endpoint
across an untrusted network.

## Local Bot API rejects a bot token

Stop the application and every other `getUpdates` consumer for this token. If
the token has already connected to Telegram's cloud Bot API, follow Telegram's
[official procedure](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server){ target="_blank" rel="noopener noreferrer" },
including the cloud `logOut` call, and wait for the migration to complete. Start
the local server first, then start the application, and keep cloud pollers
stopped while the token is assigned to the local server. `asmr-tg-backup setup`
configures the application and local service; complete token migration as the
manual step in the same rollout.

For native setup, the wheel does not install `telegram-bot-api`; build the C++
server first using the [official source instructions](https://github.com/tdlib/telegram-bot-api#installation){ target="_blank" rel="noopener noreferrer" }.

## A delivery is `uncertain`

A timeout or connection loss may occur after Telegram accepted a message. The
service records that state and does not automatically retry through MTProto,
Bot API, or splitting, because another send could create a duplicate. Inspect
the destination and local job state before deciding how to recover it.

An authorized operator can send `/panel`, open **Local resources**, select the
resource, and choose **Resolve uncertain delivery**. The panel exposes two
explicit recovery actions:

- Choose **Confirm delivered** only after verifying that the media exists in
  the target Telegram conversation. This records the local delivery, completes
  the job, and lets retention evaluation continue.
- Choose **Force resend** only after deciding to accept the risk of a duplicate
  Telegram message. The delivery job is immediately requeued.

Both actions use a state-version check and record the operator, previous error,
destination, and time in SQLite. Any intervening state change invalidates the
old confirmation. The service still never resolves `uncertain` automatically.
