# Sources and downloads

A **provider** implements remote discovery. An **origin** is one configured
channel, broadcaster, or feed handled by a provider. Use `[[origins]]` for new
configuration.

## YouTube

```toml
[[origins]]
id = "youtube-example"
provider = "youtube"
kind = "uploads"
name = "Example YouTube channel"
external_id = "UC_CHANNEL_ID"
bootstrap = "latest"
enabled = false
```

Use a real `UC...` channel ID. YouTube membership authentication and private
member discovery are outside the public-origin worker.

## Twitch

```toml
[[origins]]
id = "twitch-example"
provider = "twitch"
kind = "vods"
name = "Example Twitch broadcaster"
external_id = "broadcaster_login"
recording_mode = "vod"
bootstrap = "latest"
enabled = false
```

`external_id` may be a broadcaster login or numeric ID. Use `recording_mode =
"vod"` to download a published archive after the stream, or `"live"` to start
recording while the channel is live. Live recording requires `ffmpeg` and can
only preserve the interval during which this service is running.

Twitch credentials are read from the environment:

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

An existing `TWITCH_ACCESS_TOKEN` may be used instead of a client secret.

## RSS

```toml
[[origins]]
id = "rss-example"
provider = "rss"
kind = "feed"
name = "Example media feed"
external_id = "https://feeds.example/media.xml"
allowed_media_hosts = ["media.example"]
bootstrap = "latest"
enabled = false
```

RSS media URLs must use HTTP or HTTPS and resolve to public addresses. Set
`allowed_media_hosts` whenever the expected media hosts are known. Enable
private media addresses only for a feed you fully trust.

## Bootstrap behavior

- `bootstrap = "latest"` keeps only the newest matching first-seen item
  eligible. This is the safe default for a newly added origin.
- `bootstrap = "all"` requests a backfill. It may create substantial download
  and Telegram work.

Changing an existing origin from `latest` to `all` is an explicit backfill
request.

## Download profiles

The base `[download]` table controls yt-dlp and ffmpeg. Provider-specific tables
such as `[download.provider_profiles.twitch]` override media format and audio
extraction behavior for one provider.

The default profile keeps audio as M4A. If a profile keeps video while Telegram
is configured for audio, Telegram creates a separate audio delivery artifact
without replacing the local video master.

Downloaded masters, thumbnails, upload derivatives, and live segments remain
under the configured application data directory. See [Operate](../operations.md)
before changing that directory or deleting artifacts.
