# Changelog

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
