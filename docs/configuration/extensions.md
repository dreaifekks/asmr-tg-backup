# Extensions

Version 0.5 introduced runtime extension API level 1; version 0.6 adds the
trusted one-command setup layer. An extension is a separately
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

## One-command enablement

The two reference packages are the
[`proxy-router`](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router)
and
[`niconico-origin`](https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin)
extensions. Install only the capabilities needed by that deployment. For these
trusted entries, the normal path is one command:

```bash
asmr-tg-backup extensions enable proxy-router
asmr-tg-backup extensions enable niconico-origin
```

`enable` resolves an exact short name from the catalog bundled with the core.
It does not accept arbitrary package names or URLs. It then installs the pinned
distribution into the current pipx environment or virtual environment, checks
both runtime and setup entry points without importing them, runs the
extension-owned minimal setup, enables it, runs the same composed-runtime
checks as `doctor`, and restarts an active core-managed systemd service only
when that service uses the same main config. Repeating a healthy enable is a
no-op; use `--reconfigure` to run setup again or `--no-restart` to leave the
current process alone.

The proxy setup defaults to `127.0.0.1:7891` SOCKS5 and media-only routing, but
also accepts an HTTP/SOCKS URL or a hidden Mihomo subscription URL plus broader
scope presets. Niconico needs no extension config; the command prints its
optional ASMR live-search source suggestion without silently adding a source
that could start recording.

After an enabled extension registers a non-built-in source provider, the
Telegram panel automatically adds a matching provider button, such as
`➕ Niconico`. Selecting it asks for `<external_id> [display name]`; quote an
identifier that contains spaces. No source is created and no recording starts
until the user submits that input. The new source then appears under
`📚 Sources` as `provider/kind`.

The command never rewrites the main config. For `config.toml`, it atomically
maintains `config.extensions.toml` plus private files below `extensions/`, all
with mode `0600`. Main-config extension settings take precedence over managed
defaults. If setup, validation, or service restart fails, the previous sidecar
and private extension config are restored; a newly installed package may stay
installed but disabled.

## Manual and container installation

The following is the advanced path for image builds and deployments that do
not allow runtime package installation.

For pipx installations, inject each selected extension into the existing
application environment:

```bash
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-proxy-router==0.2.0'
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

For a virtual environment, use its interpreter:

```bash
.venv/bin/python -m pip install \
  'asmr-tg-backup-ext-proxy-router==0.2.0' \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

An official container remains minimal. Build a small derived image when an
extension is needed:

```dockerfile
FROM ghcr.io/dreaifekks/asmr-tg-backup:0.6.3
RUN python -m pip install --no-cache-dir \
    'asmr-tg-backup-ext-proxy-router==0.2.0' \
    'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

Pin exact versions or immutable commit IDs. Do not install an unreviewed
extension into a service that holds Telegram credentials or private media.

## Manual enable and validation

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

This manual configuration remains supported for third-party extensions or
deployments managed entirely by configuration management. Before restarting,
run:

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

The Telegram panel discovers enabled extension providers and offers their
default source kind through a generated button. The equivalent manual path is
to add the extension-specific origin to `sources.toml`, then apply it with the
core CLI. For the Niconico extension:

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
metadata. A runtime-only API-level-1 extension can support 0.5, while an
extension using setup API level 1 should require
`asmr-tg-backup>=0.6,<0.7`. Registration finishes before the
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

An extension can optionally keep onboarding outside its runtime object by
publishing a setup entry point with the same ID:

```toml
[project.entry-points."asmr_tg_backup.extension_setups"]
"example.provider" = "example_extension.setup:create_configurator"
```

Setup API level 1 exposes only prompt adapters, existing private configuration,
a mapping result for core-owned TOML serialization, and optional origin
suggestions. The core writes files and owns enable/doctor/restart rollback;
extension setup code never receives the database, Telegram token, or service
control.
