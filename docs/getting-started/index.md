# Choose a deployment

Both supported installations run the same Python application and use the same
TOML model. Choose based on how you want to update and supervise the process;
the Telegram upload transport can be changed independently.

| Concern | PyPI / native Linux | Docker Compose |
| --- | --- | --- |
| Installation | `pipx` or a virtual environment | Official GHCR image or local source build |
| Process supervision | `systemd --user` or another native supervisor | Compose restart policy |
| Persistent state | XDG data directory | Named `/data` volume |
| Default media upload | MTProto | MTProto |
| Local Bot API | Optional preinstalled executable and generated user unit | Optional `local-api` profile |
| OS tools | Install `ffmpeg` and `curl` yourself | Included in the image |

## Recommended path

Use [PyPI and native Linux](pypi.md) for the fewest moving pieces. The official
package can use MTProto directly, so `asmr-tg-backup setup` normally needs only
the bot token, destination, and control-panel user ID.

Use [Docker Compose](docker-compose.md) when you prefer a containerized service,
named-volume backups, or want the optional Bot API server in the same stack.

## Official releases and source builds

Official PyPI and GHCR releases provide the application identity needed by the
default MTProto path. Environment variables can override it with your own pair:

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

Set both values together. Source builds have no release defaults and therefore
need this pair (or private `[telegram.mtproto]` values) when MTProto is selected.
Alternatively, choose a Bot API transport during setup.

The bot token and MTProto session always belong to the local installation and
must remain private.

## No compatibility migration

This release uses the new nested transport configuration directly. If you have
an experimental configuration from an earlier checkout, regenerate it with
`asmr-tg-backup setup` or compare it with the packaged example instead of
expecting old flat Telegram fields to be migrated.
