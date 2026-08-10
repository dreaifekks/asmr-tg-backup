# Extensions

Version 0.5 introduces extension API level 1. An extension is a separately
installed Python distribution that registers capabilities through the
`asmr_tg_backup.extensions` entry-point group. Packages are discovered only
from the core application's Python environment and are imported only after
their IDs are explicitly enabled.

## What stays in core

Extensions do not receive a database connection, job queue, downloader, or
Telegram token. The core continues to own:

- `sources.toml` validation and atomic replacement;
- SQLite media, checkpoints, deduplication, leases, and retry state;
- live and standard worker lanes;
- yt-dlp/ffmpeg execution and tracked artifacts;
- Telegram delivery state, including the no-auto-retry `uncertain` boundary.

An extension can currently register either source definitions or networking
capabilities:

```text
installed distribution
  -> entry point -> ExtensionHost -> typed registrar -> frozen runtime
                                           |-> source provider definitions
                                           |-> one connection policy
                                           `-> one HTTP transport

source candidate -> core SQLite/jobs -> core probe/download -> core delivery
route request     -> policy lease      -> one request/process/connection
```

Multiple extensions may add distinct source provider IDs. Exactly one
connection policy and one HTTP transport may be registered for a process, so
enabling two competing network routers fails during startup instead of making
ordering decide behavior.

## Install into the same environment

The two reference packages are the
[`proxy-router`](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router)
and
[`niconico-origin`](https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin)
extensions. Install only the capabilities needed by that deployment.

For pipx installations, inject each selected extension into the existing
application environment:

```bash
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-proxy-router @ git+https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router.git@v0.1.0'
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-niconico-origin @ git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

For a virtual environment, use its interpreter:

```bash
.venv/bin/python -m pip install \
  'git+https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router.git@v0.1.0' \
  'git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

An official container remains minimal. Build a small derived image when an
extension is needed:

```dockerfile
FROM ghcr.io/dreaifekks/asmr-tg-backup:0.5.0
RUN python -m pip install --no-cache-dir \
    'git+https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router.git@v0.1.0' \
    'git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

Pin tags or immutable commit IDs. Do not install an unreviewed extension into a
service that holds Telegram credentials or private media.

## Enable and validate

`extensions list` reads distribution metadata without importing package code:

```bash
asmr-tg-backup extensions list --config config.toml
```

Enable an installed ID in the core config:

```toml
[extensions]
enabled = ["dreaife.niconico-origin"]

[extensions."dreaife.niconico-origin"]
required = true
request_timeout_seconds = 30
```

Large or secret-bearing plugin settings belong in a private file beside the
core configuration:

```toml
[extensions]
enabled = ["dreaife.proxy-router"]

[extensions."dreaife.proxy-router"]
required = true
config_file = "proxy.toml"
```

Relative `config_file` paths resolve beside `config.toml`. Inline fields are
merged over top-level fields from that file. Keep both files mode `0600`.

Before restarting, run:

```bash
asmr-tg-backup extensions doctor --config config.toml
asmr-tg-backup sources validate --config config.toml
```

`doctor` imports enabled packages, checks manifest IDs and API levels, registers
capabilities, starts and stops extension lifecycles, validates the active
source catalog with the composed provider registry, and instantiates source
adapters. A required missing package or duplicate capability is fatal. Set
`required = false` only when intentionally allowing a deployment to continue
without that optional capability.

## Add an extension source

The Telegram panel deliberately exposes only the built-in YouTube/Twitch
onboarding flow. Add extension-specific origin kinds to `sources.toml`, then
apply them with the core CLI. For the Niconico extension:

```toml
[[origins]]
id = "niconico-asmr-live"
provider = "niconico"
kind = "live_search"
name = "Niconico ASMR"
external_id = "ASMR"
enabled = true
bootstrap = "all"
max_results = 20
```

The provider emits current programs as normalized `live_stream` candidates.
The core fast-poll lane probes and records each program, persists fragments,
retries interruptions, merges completed segments, and delivers the artifact.
Some Niconico programs still require cookies, age confirmation, membership, or
payment; those yt-dlp settings belong in a private `niconico` download profile.

## Scoped connection routing

A network policy receives a `RouteRequest` and returns one `RouteLease`. The
lease is held for the complete operation:

| Scope | Lease boundary |
| --- | --- |
| `origin.resolve` | One HTML or yt-dlp source-reference resolution |
| `source.notification` | One live/feed notification request |
| `source.discovery` | One catalog/OAuth request |
| `media.probe` | One yt-dlp probe process |
| `media.download` | One complete yt-dlp process or live segment attempt |
| `telegram.control.receive` | One Bot API `getUpdates` request |
| `telegram.control.send` | One control Bot API request |
| `telegram.delivery.bot_api` | One non-idempotent curl upload |
| `telegram.delivery.mtproto` | One persistent Telethon connection |

Short idempotent HTTP operations may obtain another route on their next retry.
The core never reacquires a route inside a running yt-dlp process. A managed
gateway such as Mihomo may route a newly opened fragment connection through a
newly selected healthy subscription node, but it cannot move an existing TCP
connection. Bot API uploads never reacquire a route after sending starts, and
ambiguous outcomes remain `uncertain`. MTProto changes its core route only after
disconnecting and recreating its client connection.

Loopback HTTP endpoints are always direct. When the configured Bot API is a
local `telegram-bot-api` daemon, this extension controls only the core-to-daemon
connection. The daemon's separate Telegram egress must be routed at its own
process, container, or network namespace.

## API compatibility for extension authors

Entry-point factories return an object with an `ExtensionManifest` and
`register`, `start`, and `stop` methods:

```toml
[project.entry-points."asmr_tg_backup.extensions"]
"example.provider" = "example_extension:create_extension"
```

Use only the public types in `ytb_tg_backup.extension_api`. Set
`manifest.api_level = 1` and declare a compatible core range in package
metadata, such as `asmr-tg-backup>=0.5,<0.6`. Registration finishes before the
runtime registry is frozen; lifecycle `start` runs after core construction and
`stop` runs in reverse order. A startup failure stops extensions that already
started.

Source adapters receive a narrow `SourceAdapterContext` with a routed HTTP
client, logger, and extension data directory. Raise `SourceError` with a stable
code and optional retry delay for operational failures. Validate and normalize
origin options in the provider definition rather than reading core internals.
If every media URL from the provider needs a route capability such as
`websocket`, declare it in `SourceProviderDefinition.route_features`; a network
policy can then reject incompatible endpoints before starting yt-dlp.
