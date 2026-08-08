# 来源与下载

**推荐从 Telegram 控制面板管理来源。** 向 bot 发送 `/panel`，即可添加、启用、停用
或移除 YouTube/Twitch 来源，也可以设置过滤器和切换 Twitch 的直播/VOD 模式。同一个
bot 还接受 `/origin rename <origin_id> <name>` 和
`/origin history <origin_id>`，分别用于重命名与请求历史回填。

这些操作会原子写入 `sources.toml`，随后同步到 SQLite 的运行时镜像。它们不会改写
`config.toml`，因为后者只负责全局服务、下载、Telegram、Twitch 凭据引用和控制面板
权限。SQLite 保存轮询游标、任务、错误和媒体记录，不再作为另一套来源配置。

## 来源目录

`config.toml` 只需要指出目录文件的位置：

```toml
[sources]
path = "sources.toml"
```

相对路径以 `config.toml` 所在目录为基准；环境变量
`ASMR_TG_BACKUP_SOURCES_PATH` 可以覆盖它。`asmr-tg-backup setup` 会自动创建一份私有
目录。Compose 将它保存在宿主机的 `./settings/sources.toml`。

目录本身包含过滤器和全部来源：

```toml
version = 1
source_filter = "ASMR"

[[origins]]
id = "youtube-example"
provider = "youtube"
kind = "uploads"
name = "Example YouTube channel"
external_id = "UC_CHANNEL_ID"
enabled = true
bootstrap = "latest"

[[origins]]
id = "twitch-example"
provider = "twitch"
kind = "vods"
name = "Example Twitch broadcaster"
external_id = "broadcaster_login"
enabled = true
bootstrap = "latest"
recording_mode = "vod"
```

`source_filter` 是不区分大小写的正则表达式，会匹配来源 ID、来源名称和媒体标题；
空字符串表示不过滤。每个 `id` 都必须唯一。远端身份通常由 `provider`、`kind`、
`external_id` 组成；Twitch `vods` 另外把
`recording_mode` 计入身份，因此同一主播可以明确配置一项 `live` 和一项 `vod`，但不能
重复配置两个完全相同的来源。

## 手工精调和命令行

Panel 适合常用操作；需要修改完整字段或批量调整时，直接编辑目录文件：

```bash
asmr-tg-backup sources path
asmr-tg-backup sources export --output sources.backup.toml
# 编辑 sources.toml
asmr-tg-backup sources validate
asmr-tg-backup sources apply
asmr-tg-backup sources list
```

使用非默认配置时给命令追加 `--config /absolute/path/config.toml`。也可以先编辑另一份
文件，再用下面的命令校验并原子替换当前目录：

```bash
asmr-tg-backup sources validate --file ./candidate.toml
asmr-tg-backup sources apply --file ./candidate.toml
```

`apply` 会在一个事务中更新 SQLite 镜像；目录中被删除的来源也会从可轮询来源集合中
移除，已有媒体和任务历史仍保留。升级旧安装时运行一次
`asmr-tg-backup sources migrate`，会从已有 SQLite 来源或旧版来源声明创建目录。

旧版 `[[origins]]`、`[[channels]]` 和 `[[feeds]]` 只在 `sources.toml` 尚不存在时参与一次
迁移：当前旧声明会替代 SQLite 中过期的 config 来源，再与已有的 Panel/catalog 来源
安全合并；如果 ID 或远端身份冲突，迁移会停止并明确报错，不会暗中选择某一方优先。
`sources.toml` 一旦存在，旧声明会被忽略并产生启动警告。确认目录内容后应从
`config.toml` 删除这些声明，此后运行期只有一份来源配置真源。

常用来源字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 本地稳定标识；重命名显示名称时无需修改 |
| `provider` | `youtube` 或 `twitch` |
| `kind` | YouTube 使用 `uploads`；Twitch 支持 `vods`、`highlights`、`uploads` |
| `name` | Panel 和状态输出中的显示名称 |
| `external_id` | YouTube 的 `UC...` 频道 ID，或 Twitch 登录名/数字主播 ID |
| `enabled` | 是否参与轮询 |
| `bootstrap` | `latest` 仅从最新匹配项目开始；`all` 请求历史回填 |
| `recording_mode` | 仅 Twitch `vods`：`vod` 等待归档，`live` 在直播中录制 |

提供方特有的扁平 TOML 字段会作为来源选项保留；Panel 没有暴露的字段可以通过这条
手工路径调整。嵌套表与嵌套数组会被拒绝，以保证 Panel/CLI 重写结果稳定。

把已存在来源从 `latest` 改成 `all` 是一次明确的回填请求；已创建的历史任务不会因为
再改回 `latest` 而被撤销。

## Twitch 凭据 {#twitch-credentials}

每个 Twitch 来源都需要 `TWITCH_CLIENT_ID`，并另外提供
`TWITCH_CLIENT_SECRET` 或已有的 `TWITCH_ACCESS_TOKEN`。

1. 使用已验证邮箱并开启两步验证的账号登录
   [Twitch Developer Console](https://dev.twitch.tv/console/apps){ target="_blank" rel="noopener noreferrer" }。
2. 选择 **Register Your Application**，按照[官方应用注册说明](https://dev.twitch.tv/docs/authentication/register-app/){ target="_blank" rel="noopener noreferrer" }
   创建应用。名称需要唯一；必填的 OAuth Redirect URL 可以填写
   `http://localhost:3000`，本项目使用服务端凭据流程，不会跳转用户登录。
3. 在 **Manage** 中复制 Client ID，并选择 **New Secret** 创建 Client Secret。

PyPI/原生安装把凭据写入 `~/.config/asmr-tg-backup/env`：

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

然后运行 `chmod 600 ~/.config/asmr-tg-backup/env` 并重启用户服务。Compose 安装把相同
值写入部署目录的 `.env`，再重新创建容器。

应用会通过 Twitch 的 [client credentials flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow){ target="_blank" rel="noopener noreferrer" }
获取并刷新 app access token，不需要 Twitch 用户登录。缺少凭据时，新建的 Twitch
来源会保持停用；配置凭据并重启后再从 Panel 启用。

## 下载配置

`config.toml` 的 `[download]` 控制 yt-dlp 与 ffmpeg；
`[download.provider_profiles.twitch]` 等区块可以覆盖单个提供方的格式和音频提取行为。
默认保留 M4A 音频。若本地配置保留视频而 Telegram 发送音频，服务会另建投递文件，
不会替换视频主文件。

下载主文件、缩略图、投递派生文件和直播分段都在应用数据目录中。修改目录或删除文件前，
请阅读[运行与维护](../operations.md)。
