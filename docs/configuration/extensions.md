# Extensions

The core service is complete on its own for YouTube, Twitch, downloads, and
Telegram delivery. Add an extension when a deployment needs another source
provider or task-scoped network routing:

- enable `niconico-origin` to add a Niconico provider button to the Telegram
  Panel;
- enable `proxy-router` to choose which notification, discovery, probe,
  download, or Telegram connection scopes use a proxy;
- leave both disabled when the built-in providers and direct connections cover
  the deployment.

An extension is a separately installed Python distribution. It registers
capabilities through the `asmr_tg_backup.extensions` entry-point group and runs
in the same Python environment as the core. The core imports only the extension
IDs enabled for the active configuration. Runtime extension API level 1 was
introduced in version 0.5; version 0.6 adds the trusted one-command setup layer.

## What stays in core

The core keeps the database connection, job queue, downloader, and Telegram
token, and continues to own:

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
connection policy and one HTTP transport may be registered for a process.
Startup validation reports a capability conflict when two network routers are
enabled together.

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

`enable` resolves an exact short name from the trusted catalog bundled with the
core; third-party packages use the manual installation path below. It then
installs the pinned distribution into the current pipx environment or virtual
environment, checks both runtime and setup entry points without importing them,
runs the extension-owned minimal setup, enables it, runs the same composed-runtime
checks as `doctor`, and restarts an active core-managed systemd service only
when that service uses the same main config. Repeating a healthy enable is a
no-op; use `--reconfigure` to run setup again or `--no-restart` to leave the
current process alone.

The proxy setup defaults to `127.0.0.1:7891` SOCKS5 and media-only routing, but
also accepts an HTTP/SOCKS URL or a hidden Mihomo subscription URL plus broader
scope presets. Niconico needs no extension config; the command presents an
optional ASMR live-search suggestion, and you choose whether to add it as a
source.

After an enabled extension registers a non-built-in source provider, the
Telegram panel automatically adds a matching provider button, such as
`➕ Niconico`. Selecting it asks for `<external_id> [display name]`; quote an
identifier that contains spaces. Submit that input when you want the service
to create the source and begin polling it. The new source then appears under
`📚 Sources` as `provider/kind`. After enabling and restarting the service,
send a new `/panel` command or refresh the current active panel to regenerate
its provider buttons.

The command keeps the main config unchanged. Here `<config-stem>` means the main
config filename without its final `.toml`. It atomically maintains:

- `<config-stem>.extensions.toml` beside the main config for enabled IDs and
  managed settings;
- `extensions/<config-stem>/<filename>` beside the main config for private
  extension settings.

For the default `config.toml`, these paths become `config.extensions.toml` and
`extensions/config/<filename>`. Managed files use mode `0600`, and explicit
settings in the main config take precedence. If setup, validation, or service
restart fails, the command restores the previous managed state and private
settings; the installed package remains available for a later enable attempt.

## Native manual installation

The one-command path is preferred for native installations. When configuration
management owns package installation, inject each selected extension into the
same environment as the core.

For pipx:

```bash
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-proxy-router==0.2.0'
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

For a virtual environment:

```bash
.venv/bin/python -m pip install \
  'asmr-tg-backup-ext-proxy-router==0.2.0' \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

Continue with [manual enable and validation](#manual-enable-and-validation)
when using this package-managed path.

## Install extensions in Docker Compose {#docker-extensions}

A Docker installation has two durable layers:

1. the derived image contains the extension Python packages;
2. host-mounted configuration selects extension IDs and supplies private
   settings.

Recreating the container then produces the same extension runtime every time.
Installing a package from a shell inside an existing container only changes
that one container and is lost when Compose replaces it.

The two trusted extensions use these exact package names and runtime IDs:

| Capability | Package installed in the image | ID enabled in `config.toml` | Private file |
| --- | --- | --- | --- |
| Proxy routing | `asmr-tg-backup-ext-proxy-router==0.2.0` | `dreaife.proxy-router` | Required |
| Niconico source | `asmr-tg-backup-ext-niconico-origin==0.2.0` | `dreaife.niconico-origin` | Not required |

### 1. Create the derived image

In the Compose checkout, create `Dockerfile.extensions`:

```dockerfile
ARG CORE_VERSION=0.6.4
FROM ghcr.io/dreaifekks/asmr-tg-backup:${CORE_VERSION}

ARG PROXY_ROUTER_VERSION=0.2.0
ARG NICONICO_ORIGIN_VERSION=0.2.0

RUN python -m pip install --no-cache-dir \
    "asmr-tg-backup-ext-proxy-router==${PROXY_ROUTER_VERSION}" \
    "asmr-tg-backup-ext-niconico-origin==${NICONICO_ORIGIN_VERSION}"
```

Keep only the package lines needed by this deployment. Build an immutable local
tag so upgrades and rollbacks remain explicit:

```bash
docker build --pull \
  --build-arg CORE_VERSION=0.6.4 \
  --build-arg PROXY_ROUTER_VERSION=0.2.0 \
  --build-arg NICONICO_ORIGIN_VERSION=0.2.0 \
  -f Dockerfile.extensions \
  -t asmr-tg-backup:0.6.4-extensions .
```

Select that image in `.env`:

```dotenv
ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:0.6.4-extensions
```

### 2. Enable the installed IDs

Edit the existing `[extensions]` section in the host `config.toml`. This example
enables both packages:

```toml
[extensions]
enabled = [
  "dreaife.proxy-router",
  "dreaife.niconico-origin",
]

[extensions."dreaife.proxy-router"]
required = true
config_file = "extensions/proxy-router.toml"

[extensions."dreaife.niconico-origin"]
required = true
```

Keep only the IDs installed in the image. The package name belongs in the
Dockerfile; the runtime ID belongs in `config.toml`.

### 3. Create the proxy settings when needed

Niconico needs no private extension file, so a Niconico-only image can skip
this step. For proxy routing, create the host directory and private file:

```bash
mkdir -p extensions
chmod 700 extensions
# Create extensions/proxy-router.toml with the settings below.
chmod 600 extensions/proxy-router.toml
```

A media-only HTTP/SOCKS example is:

```toml
fail_closed = true
routes = [
  "http://host.docker.internal:7890",
  "socks5h://host.docker.internal:7891",
]

[scopes]
"media.probe" = "proxy"
"media.download" = "proxy"
```

Inside the application container, `127.0.0.1` means the container itself.
`compose.yaml` already maps `host.docker.internal` to the Docker host, but the
host proxy must listen on an address reachable from the Docker bridge. Keep
that listener limited to trusted local/Docker networks. When the proxy is
another Compose service, use its service name instead. The
[`proxy-router` package guide](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router)
covers subscriptions and every routing scope.

### 4. Mount the private settings

Create `compose.extensions.yaml`:

```yaml
services:
  asmr-tg-backup:
    volumes:
      - ./extensions:/config/extensions:ro
```

Add the override to `.env` so every Compose command uses it:

```dotenv
COMPOSE_FILE=compose.yaml:compose.extensions.yaml
```

If `COMPOSE_FILE` already contains another override, append
`:compose.extensions.yaml` instead of replacing the existing value. A
Niconico-only deployment can omit this override because it has no private file.

### 5. Validate and start

Confirm that Compose resolves the derived tag, then inspect and validate the
composed extension runtime before starting the worker:

```bash
docker compose config --images
docker compose run --rm asmr-tg-backup \
  extensions list --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

`extensions list` should report each selected ID as installed and enabled.
After all three checks pass, start the exact image already built:

```bash
docker compose up -d --no-build asmr-tg-backup
docker compose logs --tail=100 asmr-tg-backup
```

For Niconico, send a new `/panel` or refresh the active Panel after startup and
confirm that `➕ Niconico` is present. For proxy routing, inspect the service log
for the selected policy without printing the private endpoint or subscription.

### Upgrade or roll back

For an upgrade, change the core and extension version arguments, build a new
image tag, point `ASMR_TG_BACKUP_IMAGE` at it, and rerun the three validation
commands before recreation. Keep the previous local tag until the updated
service has been verified; rollback consists of selecting that previous tag and
recreating the application with `--no-build`.

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
