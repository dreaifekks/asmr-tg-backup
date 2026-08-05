# Development YouTube membership observer

This is a development-only, anonymous, metadata-only experiment for measuring
where YouTube member videos and streams become discoverable. It never downloads
media, uses account authentication, reads cookies, or uploads anything to
Telegram. Its state is separate from the production backup worker.

The observer compares these surfaces:

- the anonymous official channel Atom feed;
- the anonymous, automatically maintained members-only playlist (`UUMO...`);
- the anonymous channel `streams` and `videos` tabs through flat `yt-dlp`
  extraction;
- a metadata-only `yt-dlp --skip-download` probe for newly discovered IDs.

Every `yt-dlp` invocation uses `--ignore-config`, so a user-level yt-dlp config
cannot silently add cookies. A legacy `[auth]` table is accepted only when
`enabled = false` and `extra_args = []`; enabling it or adding any argument is a
configuration error.

The first successful poll is a baseline. It probes a limited number of entries
from the members-only playlist plus titles with an explicit membership marker.
Later polls probe each ID the first time it appears on a surface, even when the
title has no marker. Transient network/tool failures are retried. Removed,
unavailable, private, and generic `no_formats` results are terminal and leave
the retry queue. The members-only
playlist is an observed, undocumented YouTube web surface, so Atom and
channel-tab comparisons remain enabled.

Probe candidates are persisted before execution. If a challenge, shutdown, or
cooldown interrupts a batch, the remaining IDs stay queued even after they fall
out of the Feed/tab window. `retry_probe_limit_per_channel` bounds that recovery
work per channel and cycle; `totals.pending_probes` exposes the backlog.

## Local run

```bash
cp config.member-observer.example.toml members-observer.local.toml
chmod 600 members-observer.local.toml
# Edit the channel IDs and set a future UTC stop_at first.
.venv/bin/python -m ytb_tg_backup.dev.member_observer once --config members-observer.local.toml
.venv/bin/python -m ytb_tg_backup.dev.member_observer report --config members-observer.local.toml
.venv/bin/python -m ytb_tg_backup.dev.member_observer report --config members-observer.local.toml --output work/member-observer/snapshot.json
```

The old `python -m ytb_tg_backup.member_observer` path remains as a compatibility
shim, but new scripts and services should use the `dev` module path above.

The SQLite database and `events.jsonl` are written below `observer.data_dir`.
The observer enforces mode `0700` on that directory and `0600` on local output
files. The local config, `work/`, and database files are ignored by Git. Do not
run `once` against the same data directory while `run` is active; a lock rejects
that second writer. The read-only `report` command can run alongside the service.

## User systemd service

```bash
install -Dm600 deploy/ytb-member-observer.service ~/.config/systemd/user/ytb-member-observer.service
systemctl --user daemon-reload
systemctl --user enable --now ytb-member-observer.service
systemctl --user status ytb-member-observer.service
journalctl --user -u ytb-member-observer.service -f
loginctl show-user "$USER" -p Linger
```

Stop it with:

```bash
systemctl --user disable --now ytb-member-observer.service
```

Set a fixed UTC `observer.stop_at` in the local config so restarts cannot extend
the experiment indefinitely. When the deadline is reached, the observer writes
`final-report.json` beside the database and exits successfully.

The provided unit assumes the repository is at `~/dev/ytb-tg-backup`; edit
`WorkingDirectory` and `ExecStart` when installing elsewhere. A user service
also needs `Linger=yes` to survive logout. Check collection health with the
`report` command: `latest_cycle.success` should be `1`, `finished_at` should be
recent, `interrupted` should be `0`, and `latest_surface_runs` should explain
every channel/surface as `success`, `empty`, `error`, or `skipped`. An active
systemd process alone does not prove that YouTube requests are succeeding.
`latest_cycle` always means the latest completed cycle; `active_cycle` is
non-null while a newer cycle is still collecting.

## Anonymous-only safety boundary

Authentication is intentionally unsupported, including `--cookies`,
`--cookies-from-browser`, and authenticated extractor arguments. The observer
persists a global cooldown and stops subsequent requests after rate-limit or
bot-check signals. It stores normalized metadata and bounded error text, not
request headers, media data, or complete `yt-dlp` debug output.

`surface_item_counts` and `channel_coverage` are cumulative unique counts, not a
snapshot of what is currently visible. A video falling beyond `tab_limit` does
not prove deletion; use successful per-cycle `sightings` and `surface_runs` when
interpreting appearance or disappearance.
