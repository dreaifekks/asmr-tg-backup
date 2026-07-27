# Changelog

## Unreleased

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
