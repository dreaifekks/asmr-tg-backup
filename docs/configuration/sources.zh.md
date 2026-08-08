# 来源与下载

**提供方（provider）**负责从远端发现内容；**来源（origin）**则是由某个提供方处理的
频道、主播或订阅源。新配置请使用 `[[origins]]`。

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

请填写真实的 `UC...` 频道 ID。YouTube 会员身份验证和私有会员内容发现不属于公开
来源 worker 的功能范围。

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

`external_id` 可以是主播登录名或数字 ID。使用 `recording_mode = "vod"` 会在直播
归档发布后下载；使用 `"live"` 则在频道直播时开始录制。直播录制需要 `ffmpeg`，
而且只能保存本服务正在运行期间的内容。

Twitch 凭据从环境变量读取：

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

也可以使用已有的 `TWITCH_ACCESS_TOKEN` 代替 client secret。

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

RSS 媒体 URL 必须使用 HTTP 或 HTTPS，并解析到公网地址。如果已知预期的媒体主机，
请设置 `allowed_media_hosts`。只有在完全信任订阅源时，才允许访问私有网络中的媒体地址。

## Bootstrap 行为

- `bootstrap = "latest"` 只让首次发现时最新的一条匹配内容进入处理流程。这是新来源的
  安全默认值。
- `bootstrap = "all"` 会请求回填历史内容，可能创建大量下载和 Telegram 任务。

把已有来源从 `latest` 改为 `all`，即表示明确请求回填。

## 下载配置档

基础 `[download]` 表控制 yt-dlp 和 ffmpeg。诸如
`[download.provider_profiles.twitch]` 的提供方配置表，可以针对单个提供方覆盖媒体
格式和音频提取行为。

默认配置档把音频保留为 M4A。如果某个配置档保留视频，而 Telegram 配置为发送音频，
Telegram 会创建独立的音频投递文件，不会替换本地视频主文件。

下载主文件、缩略图、上传衍生文件和直播分段都会保存在配置的应用数据目录下。修改目录
或删除文件前，请先阅读[运行与维护](../operations.md)。
