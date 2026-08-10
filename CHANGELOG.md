# Changelog

## Unreleased

## 0.6.1 - 2026-08-10

- Add extension-aware Telegram source controls. Enabled non-built-in source
  providers automatically receive an add button, while creation, duplicate
  detection, and live-safe bootstrap use the provider's registered contract.

## 0.6.0 - 2026-08-10

- Add `extensions enable <short-name>` as the primary trusted-extension flow.
  It resolves only the built-in catalog, injects the package into the current
  pipx/virtualenv, runs the extension-owned minimal setup, writes a private
  managed sidecar without rewriting `config.toml`, validates the composed
  runtime, and restarts only an active managed service using the same config.
- Add a separate setup entry-point API so optional repositories own their
  onboarding. The proxy router offers SOCKS5, HTTP/SOCKS URL, and hidden Mihomo
  subscription setup with scope presets; Niconico remains zero-config and
  exposes an optional source suggestion. Failed validation or restart restores
  the previous managed state and private extension config.

## 0.5.0 - 2026-08-09

- Add an API-level-1 extension host based on Python entry points. Enabled
  packages can register typed source providers or one process-wide connection
  policy/HTTP transport, while core retains ownership of SQLite, jobs,
  deduplication, retries, downloads, and Telegram delivery uncertainty.
- Route provider resolution, notification/discovery polling, yt-dlp probes and
  downloads, Telegram control traffic, Bot API uploads, and MTProto connections
  through task-scoped route leases. Explicit direct routes remove inherited
  proxy variables, and loopback Bot API endpoints remain forced direct.
- Generalize the former Twitch-only fast polling/recording lane into provider-
  neutral live scheduling so extension origins can emit `live_stream` items
  without taking over workers or durable state.
- Add `extensions list` for import-free package discovery and
  `extensions doctor` for enabled-extension loading, lifecycle startup, source
  catalog validation, and composed-runtime checks. Keep extensions disabled by
  default and support private per-extension TOML files.
- Publish matching optional repositories for scoped HTTP/SOCKS/Mihomo
  subscription routing and Niconico live-search discovery. The core package
  does not acquire their proxy, WebSocket, or site-specific dependencies.

## 0.4.1 - 2026-08-08

- Add `asmr-tg-backup service install` and `service uninstall` for generated
  systemd user-service registration, immediate startup, user linger, and
  data-preserving removal.
- Make `sources.toml` the single user-editable source catalog. Telegram panel
  and source CLI changes now update it atomically before reconciling the SQLite
  runtime mirror; guided setup creates it, and Compose exposes it through the
  writable `./settings` directory.
- Add `asmr-tg-backup sources path`, `list`, `validate`, `apply`, `export`, and
  `migrate` for inspecting, manually tuning, snapshotting, and upgrading source
  configuration without editing runtime database tables.
- Refocus the README and bilingual documentation on YouTube/Twitch users and
  external contributors, add the public Telegram showcase and Telegram setup
  shortcuts, explain the panel-first source workflow and Twitch application
  credentials, add a bilingual contribution guide, document the current
  MTProto-first architecture, and retire outdated source-checkout setup guides.

## 0.4.0 - 2026-08-08

- Split cloud-API audio uploads into independently playable parts and submit
  them as one Telegram media group. Each part now has a distinct `Part i/n`
  audio title and an independently attached cover, while preserving quality
  and the existing uncertain-delivery protection for ambiguous outcomes.
- Add direct MTProto media upload as the default transport, with a persistent
  private session, a 1.99 GB application safety limit, runtime application
  credential overrides, and optional `cryptg` acceleration. Keep Bot API as an
  explicit advanced transport and playable cloud-API splitting as its final
  oversize fallback.
- Add a container image and Compose deployment with persistent MTProto state,
  while retaining an optional local Telegram Bot API profile, custom API
  endpoints, and direct loopback-safe native deployments.
- Force loopback Telegram upload and control requests to bypass inherited HTTP
  proxies so bot-token URLs stay on the local host.
- Prepare the Python distribution for PyPI with a packaged safe config
  template, Telethon and yt-dlp dependencies, build and Trusted Publishing
  checks, and an official GHCR image built from the same verified wheel. Add an
  interactive one-time setup that defaults to MTProto and exposes existing,
  local, and official-splitting Bot API paths as advanced choices. The wheel
  never bundles or downloads the C++ server executable.
- Add a bilingual English/Simplified Chinese MkDocs Material documentation
  site covering deployment, configuration, operation, architecture, security,
  and troubleshooting.

## 0.3.2 - 2026-08-02

- Preserve compatible source audio when preparing Telegram audio from a video
  master, avoiding an unnecessary low-bitrate AAC transcode for Twitch live
  recordings and other retained-video profiles.
- Raise the adaptive AAC fallback ceiling from 64 kbps to 256 kbps when the
  upload limit permits, while retaining lower bitrate candidates for genuinely
  constrained deliveries.
- Write audio-copy and fallback-encoding attempts through temporary files so
  incompatible or oversized candidates do not leave partial artifacts behind.

## 0.3.1 - 2026-07-29

- Fix Telegram filenames for Twitch live recordings by using the persisted
  media publication date after live merge or audio compression, avoiding the
  `unknown-date` fallback when the derived artifact name has no date prefix.

## 0.3.0 - 2026-07-27

- Add a paginated, searchable local ASMR resource library to the Telegram
  control panel, including artifact health, delivery state, and tracked-file
  details for completed and interrupted live recordings.
- Add opt-in, confirmation-gated local resource purging that deletes only
  exact tracked files below the downloads root, blocks unsafe or active
  resources, cancels retryable work, and preserves database audit records.
- Harden artifact deletion against concurrent writers and interrupted purges
  with SQLite path reservations, tombstones, and retryable recovery state.
- Add a self-contained `AGENT_QUICK_START.md` that an agent can fetch with
  `curl` to configure and verify a basic local service, linked from the README.
- Rename the public project, CLI, deployment templates, documentation, and
  request identity from `ytb-tg-backup` to the provider-neutral
  `asmr-tg-backup`, while retaining the Python import namespace for compatibility.

## 0.2.2 - 2026-07-27

- Make each explicit `/panel` or `/start` command send a fresh Telegram panel
  below the command while retiring the previous panel's buttons.
- Preserve YouTube upcoming/live metadata when formats are not ready so
  scheduled streams wait without exhausting the download failure budget.

## 0.2.1 - 2026-07-25

- Add configurable Twitch `vod` versus `live` recording. Live mode polls Helix
  Get Streams on a dedicated fast schedule and records the channel immediately
  with yt-dlp, avoiding subscriber-only VOD lockout after the broadcast.
- Let the Telegram panel choose `live` or `vod` when adding each Twitch channel,
  display the effective mode, and switch bot-managed channels without restart.
- Expire Telegram control panels after one idle hour by default, remove the old
  inline keyboard, and reject callbacks from closed or superseded messages.
- Isolate long-running Twitch recordings from normal downloads and Telegram
  delivery with a dedicated worker lane, unlimited-by-default live timeout,
  lease-aware process cancellation, and duplicate VOD suppression by stream ID.
- Reset stale poll checkpoints when switching recording modes, reconnect the
  same active stream without consuming its failure budget, and arbitrate
  live/VOD completion transactionally to prevent duplicate delivery.
- Preserve finalized fragments across service stops and transient network
  failures, normalize mixed interrupted/finished containers, then merge the
  recording segments when the stream ends.

## 0.2.0 - 2026-07-22

- Generalize discovery around provider-backed origins for YouTube, RSS, and
  Twitch public VODs while retaining legacy channel/feed configuration.
- Store provider-neutral media, origin associations, leased jobs, audio
  artifacts, and Telegram deliveries in the versioned SQLite v2 schema, with
  automatic and backed-up migration from v1.
- Add provider-specific audio-only download profiles. Twitch archives the best
  available audio as M4A and does not retain source video.
- Add a persistent single-message Telegram control panel for origin management,
  filtering, and status, backed by a trigger-invalidated materialized snapshot.
- Move Telegram updates to an independent long-poll worker so panel actions no
  longer wait for the former periodic control cycle.
- Add bounded provider, subprocess, and Telegram timeouts; harden URL handling,
  runtime file permissions, secret handling, and the user systemd service.

YouTube members-only discovery and authentication are not part of this release.
Twitch support covers public archived broadcasts exposed by the Helix API.

## 0.1.0

- Initial YouTube feed polling, audio archive, Telegram delivery, and control
  command implementation.
