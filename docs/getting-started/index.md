# Choose a deployment

PyPI and Docker Compose run the same application and use the same TOML
configuration. Pick the installation style you prefer; the Telegram upload
method can be changed independently.

| | PyPI / native Linux | Docker Compose |
| --- | --- | --- |
| Best for | A lightweight service on one Linux host | A container-managed stack |
| Process manager | `systemd --user` | Compose restart policy |
| Source configuration | `~/.config/asmr-tg-backup/sources.toml` | `./settings/sources.toml` |
| Runtime state | XDG data directory | `asmr-data` volume mounted at `/data` |
| Default upload | MTProto | MTProto |
| Media tools | Install `ffmpeg`/`ffprobe` | Included in the image |

## Telegram shortcuts {#telegram-shortcuts}

<div class="grid cards" markdown>

-   **Create a bot**

    [Open BotFather](https://t.me/BotFather){ target="_blank" rel="noopener noreferrer" }

    Create the bot, copy its token, then add it to the destination channel with
    permission to post.

-   **Find your user ID**

    [Open @userinfobot](https://t.me/userinfobot){ target="_blank" rel="noopener noreferrer" }

    Copy the numeric ID used to authorize the Telegram control panel.

-   **Create a Telegram API ID/hash**

    [Open Telegram API management](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" }

    This is needed for a source build with its own MTProto application or for a
    local Bot API server. Official PyPI and GHCR installations are ready to use
    MTProto without this step.

</div>

Also prepare the destination chat ID or a public `@channel` name.

MTProto signs in as the bot and creates a reusable session during the first
delivery. It does not require a personal account or phone verification code.

## Pick an installation

- [PyPI and native Linux](pypi.md) is the shortest path: install the package,
  run `asmr-tg-backup setup`, then register it with
  `asmr-tg-backup service install`.
- [Docker Compose](docker-compose.md) keeps the application and its data in a
  container-managed stack.

## Pick an upload method

| Method | When to use it |
| --- | --- |
| MTProto | Default. Uploads the file directly without a separate Bot API server. |
| Existing Bot API URL | Connect to a Bot API endpoint you already run. |
| Local Bot API | Run `telegram-bot-api` through native systemd or the Compose `local-api` profile. |
| Telegram cloud Bot API | Uses a 49 MB per-file setting and splits oversized audio into playable parts. |

## What happens after setup

The PyPI setup asks for the bot token, destination, and control-panel user ID.
It creates `config.toml`, `sources.toml`, and the SQLite database, but does not
add any media sources. Start the service, send `/panel` to the bot, and add one
YouTube or Twitch source. The panel writes sources and the filter to the
editable `sources.toml`; SQLite contains only the synchronized mirror and
runtime state. Twitch sources also need application credentials from the
[Twitch setup guide](../configuration/sources.md#twitch-credentials).

Compose uses `.env`, `config.toml`, and `settings/sources.toml` instead of the
interactive setup command.

Source builds that use MTProto need their own complete API ID/hash pair. See
[Architecture and development](../development.md) for the source workflow.
