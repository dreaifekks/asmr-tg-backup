# asmr-tg-backup Agent Quick Start

You are the local setup agent. Read this entire document before taking action,
then use it to configure a basic `asmr-tg-backup` service on a Linux host with
user-level systemd. The service discovers ASMR media from YouTube, Twitch, or
RSS, archives audio with `yt-dlp`, and can deliver it to Telegram.

## Expected result

- Checkout: `~/dev/asmr-tg-backup`
- Private config: `~/.config/asmr-tg-backup/config.toml`
- Optional credentials/proxy environment: `~/.config/asmr-tg-backup/env`
- State and downloads: `~/.local/share/asmr-tg-backup`
- User service: `asmr-tg-backup.service`

The public CLI is `asmr-tg-backup`; the compatibility Python module remains
`ytb_tg_backup`.

## Safety rules

1. Ask before installing OS packages, using networked package installers,
   enabling real origins, starting downloads, or sending anything to Telegram.
2. Never print, commit, or paste secrets into logs. Keep the real config and
   environment files at mode `0600`.
3. If a checkout already exists, inspect `git status --short --branch`. Do not
   discard, overwrite, or broadly stage unrelated changes.
4. Start with origins and Telegram disabled. Initialize and verify the service
   first, then enable real activity only after the user confirms the targets.

## Inputs to collect

Ask the user for only the inputs needed by the selected providers:

- One or more origins:
  - YouTube: a real `UC...` channel ID.
  - Twitch: broadcaster login or numeric ID, plus `vod` or `live` mode.
  - RSS: public feed URL and, preferably, allowed media hosts.
- Telegram delivery, if wanted:
  - bot token;
  - destination chat/channel ID such as `-100...` or `@channel`.
- Telegram control panel, if wanted:
  - allowed user ID;
  - optional allowed chat and message-thread IDs.
- Twitch, if used:
  - `TWITCH_CLIENT_ID`;
  - either `TWITCH_ACCESS_TOKEN` or `TWITCH_CLIENT_SECRET`.
- Media proxy, if wanted:
  - proxy protocol and address;
  - whether it must cover source discovery/APIs, downloads, or both.

Do not request Telegram, Twitch, or proxy credentials when those features are
not needed. If a proxy URL contains credentials, have the user store it
privately through `url_env`; do not ask them to paste the secret into chat.

## 1. Inspect prerequisites

```bash
uname -a
python3 --version
command -v git
command -v curl
command -v ffmpeg
command -v ffprobe
```

Required:

- Python 3.11 or newer;
- Git;
- `yt-dlp`.

`curl` is required for Telegram delivery. `ffmpeg` and `ffprobe` are strongly
recommended and `ffmpeg` is required for Twitch live recording. If a required
tool is missing, explain the package-manager command and get approval before
installing it.

## 2. Get the repository

```bash
mkdir -p ~/dev
git clone https://github.com/dreaifekks/asmr-tg-backup.git ~/dev/asmr-tg-backup
cd ~/dev/asmr-tg-backup
git status --short --branch
```

If the checkout already exists, do not clone over it. Verify its remote and
working tree before deciding whether a pull is safe:

```bash
git remote -v
git status --short --branch
```

## 3. Create the Python environment

The project has no required runtime Python libraries, but the editable install
provides the `asmr-tg-backup` command. With approval for package downloads:

```bash
cd ~/dev/asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools
.venv/bin/python -m pip install -e . yt-dlp
.venv/bin/asmr-tg-backup --help
```

If editable installation is unavailable, the service can still run with
`PYTHONPATH=src` and `python -m ytb_tg_backup`; fix the deployment unit
accordingly instead of hiding the failure.

## 4. Create the private configuration

```bash
mkdir -p ~/.config/asmr-tg-backup
chmod 700 ~/.config/asmr-tg-backup
cp ~/dev/asmr-tg-backup/config.example.toml \
  ~/.config/asmr-tg-backup/config.toml
chmod 600 ~/.config/asmr-tg-backup/config.toml
```

Edit `~/.config/asmr-tg-backup/config.toml`. Update the existing keys shown
below; do not append duplicate TOML tables. Keep this base:

```toml
[app]
data_dir = "~/.local/share/asmr-tg-backup"
poll_interval_seconds = 1800
download_delay_seconds = 300
max_items_per_poll = 3
worker_count = 1
log_level = "INFO"

[proxy]
url = ""
url_env = ""
sources = false
downloads = false

[download]
yt_dlp = "/home/USERNAME/dev/asmr-tg-backup/.venv/bin/yt-dlp"
ffmpeg = "ffmpeg"
format = "bestaudio/best"
extract_audio = true
audio_format = "m4a"
write_info_json = true
write_thumbnail = true

[telegram]
enabled = false
bot_token = ""
chat_id = ""

[control]
enabled = false
allowed_user_ids = []
allowed_chat_ids = []
allowed_message_thread_ids = []
```

Replace `USERNAME` with the actual non-root user. The full copied example
contains retry, timeout, provider-profile, and caption defaults; preserve those
sections unless the user requests a change.

Add or edit origins, keeping them disabled during setup.

YouTube:

```toml
[[origins]]
id = "youtube-asmr"
provider = "youtube"
kind = "uploads"
name = "YouTube ASMR"
external_id = "UC_CHANNEL_ID"
bootstrap = "latest"
enabled = false
```

Twitch:

```toml
[[origins]]
id = "twitch-asmr"
provider = "twitch"
kind = "vods"
name = "Twitch ASMR"
external_id = "broadcaster_login"
bootstrap = "latest"
enabled = false
recording_mode = "vod" # use "live" only when requested
```

RSS:

```toml
[[origins]]
id = "rss-asmr"
provider = "rss"
kind = "feed"
name = "ASMR feed"
external_id = "https://feeds.example/media.xml"
allowed_media_hosts = ["media.example"]
enabled = false
```

Use `bootstrap = "latest"` by default. Use `"all"` only when the user
explicitly wants a backfill.

## 5. Configure optional proxy, delivery, and control

The copied public config keeps both scopes `false`; proxying is an explicit
opt-in. Leave both URL fields empty and both scopes disabled when no
application-scoped proxy is wanted. When a proxy is requested for the complete
media path, prefer an HTTP proxy and update the existing table:

```toml
[proxy]
url = "http://127.0.0.1:7890"
url_env = ""
sources = true
downloads = true
```

This applies to source discovery/provider APIs and yt-dlp, but `[proxy]` is not
injected into Telegram. In particular, a local `[telegram].api_base` such as
`http://127.0.0.1:18081` is unaffected. Do not substitute process-wide
`HTTP_PROXY` variables for this scoped configuration; those variables also
retain their normal effect on urllib and curl.

SOCKS is downloads-only and must use `sources = false`. Twitch live HLS may
delegate traffic to ffmpeg, so SOCKS is not guaranteed for live recording;
prefer HTTP when live traffic must use the proxy.

For a proxy URL containing credentials, leave `url` empty and name one
environment variable instead:

```toml
[proxy]
url = ""
url_env = "ASMR_TG_BACKUP_PROXY"
sources = true
downloads = true
```

Set only one of `url` or `url_env`. A configured `url_env` must exist when the
service starts; verify that its value is non-empty before enabling real traffic.

For Telegram delivery, set the real values only in the private config. Keep it
disabled during setup:

```toml
[telegram]
enabled = false # switch to true only after explicit approval
bot_token = "BOT_TOKEN"
chat_id = "-1001234567890"
api_base = "https://api.telegram.org"
media_type = "audio"
```

For the Telegram control panel, keep it disabled during setup:

```toml
[control]
enabled = false # switch to true only after explicit approval
allowed_user_ids = ["123456789"]
allowed_chat_ids = []
allowed_message_thread_ids = []
```

Every configured authorization dimension must match. Prefer an allowed user ID;
do not leave control enabled with all allowlists empty.

For Twitch and credentialed proxy URLs, store sensitive values outside TOML:

```bash
touch ~/.config/asmr-tg-backup/env
chmod 600 ~/.config/asmr-tg-backup/env
```

The environment file contains:

```dotenv
TWITCH_CLIENT_ID=...
TWITCH_ACCESS_TOKEN=...
# Or use TWITCH_CLIENT_SECRET instead of TWITCH_ACCESS_TOKEN.
# If [proxy].url_env names this variable:
ASMR_TG_BACKUP_PROXY=http://user:password@proxy.example:8080
```

Never commit or echo this file.

## 6. Initialize without polling

Initialization creates the SQLite schema and data directories but does not poll
providers or send Telegram messages:

```bash
cd ~/dev/asmr-tg-backup
.venv/bin/python -m ytb_tg_backup init \
  --config ~/.config/asmr-tg-backup/config.toml
.venv/bin/python -m ytb_tg_backup status \
  --config ~/.config/asmr-tg-backup/config.toml
```

Confirm that `state.db` exists under
`~/.local/share/asmr-tg-backup/` and that the config/database permissions are
not group- or world-readable.

## 7. Install the user service

The shipped unit assumes the checkout and config paths used above:

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 ~/dev/asmr-tg-backup/deploy/asmr-tg-backup.service \
  ~/.config/systemd/user/asmr-tg-backup.service
systemctl --user daemon-reload
systemctl --user enable asmr-tg-backup.service
```

Before starting it, show the user the enabled origins and whether Telegram and
control are enabled. Start only after confirmation:

```bash
systemctl --user start asmr-tg-backup.service
```

## 8. Verify

```bash
systemctl --user show asmr-tg-backup.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts
journalctl --user -u asmr-tg-backup.service --no-pager -n 80
.venv/bin/python -m ytb_tg_backup status \
  --config ~/.config/asmr-tg-backup/config.toml
```

Success means:

- `ActiveState=active` and `SubState=running`;
- no startup/config/authorization errors in the journal;
- the status command can read the initialized database;
- no real provider or Telegram action occurred before explicit confirmation.

If the user then approves live operation, enable the intended origins and any
approved Telegram delivery/control features in the private config, then restart
the service:

```bash
systemctl --user restart asmr-tg-backup.service
```

Report the final checkout, config, data, and unit paths; enabled providers and
delivery/control state; service status; and any skipped prerequisite. Never
include secret values in the report.
