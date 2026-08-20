# 来源与下载

来源决定服务从哪里发现媒体，以及如何轮询每一个 Origin。日常调整可以在 Telegram
Panel 中完成；需要完整字段、RSS 来源或批量修改时，使用 `sources.toml`。

## 可以管理哪些内容

通过 `/panel` 可以：

- 添加 YouTube 频道和 Twitch 主播；
- 添加已启用扩展注册的提供方，例如 Niconico；
- 启用、停用、查看或移除已有来源；
- 为 Twitch VOD 来源选择直播录制或结束后下载；
- 查看、设置、关闭或重置全局来源过滤器。

扩展注册来源提供方后，启用扩展并重启服务，Panel 会出现对应的
`➕ 提供方`按钮。需要创建来源并开始轮询时，选择该按钮并提交来源标识。

RSS 是内置的手工目录来源，需要写入 `sources.toml`，再通过 CLI 应用。

## 添加前需要准备什么

编辑和备份服务时，按下面的职责保存各项数据：

- `sources.toml` 保存可编辑的来源目录和全局来源过滤器；
- `config.toml` 保存服务、下载、Telegram、凭据引用、手工管理的扩展和 Panel 权限配置；
- 一键扩展 setup 把受管状态保存在 `<config-stem>.extensions.toml`，私密设置保存在
  `extensions/<config-stem>/`；
- SQLite 保存同步后的运行时镜像，以及轮询游标、任务、错误和媒体记录。

这里的 `<config-stem>` 表示去掉末尾 `.toml` 后的主配置文件名。
Panel 与 CLI 都会原子更新 `sources.toml`，随后同步 SQLite。需要调整来源时编辑目录，
无需直接修改 SQLite 中的来源行。

添加前先准备对应的远端标识：YouTube handle 或频道 ID、Twitch 登录名或 user ID、
扩展定义的标识，或者 RSS feed URL。Twitch 还需要按
[Twitch 凭据](#twitch-credentials)配置访问凭据。添加完成后检查来源过滤器；即使来源
连接正常，标题、来源名称和来源 ID 都不匹配过滤器的项目仍会被跳过。

## 从 Telegram 添加来源

1. 如果提供方来自扩展，先启用并配置该扩展。例如：

   ```bash
   asmr-tg-backup extensions enable niconico-origin
   ```

2. 发送 `/panel`，然后选择 `➕ YouTube`、`➕ Twitch` 或扩展增加的提供方按钮。
3. 按提示输入。扩展提供方使用 `<external_id> [显示名称]`；标识中包含空格时用引号
   包住。
4. 打开 `📚 来源`，确认新来源、启用状态和 `provider/kind`。

同一个 bot 还接受 `/origin rename <origin_id> <name>` 和
`/origin history <origin_id>`，分别用于重命名与请求历史回填。扩展安装及提供方示例见
[扩展](extensions.md)。

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

### 手工添加 RSS Feed {#rss-feed}

RSS 来源的 `external_id` 填写完整 feed URL，`kind` 使用 `feed`：

```toml
[[origins]]
id = "rss-example"
provider = "rss"
kind = "feed"
name = "Example media feed"
external_id = "https://feeds.example.com/media.xml"
enabled = true
bootstrap = "latest"
allowed_media_hosts = ["media.example.com", "*.cdn.example.com"]
allow_private_media = false
```

每个 RSS item 或 Atom entry 都会把自己的 link 作为媒体 URL。这个地址必须使用 HTTP
或 HTTPS，并且不能内嵌凭据。默认情况下，媒体主机必须解析到公网地址。
`allowed_media_hosts` 会把下载范围进一步限制为列出的准确主机和通配子域名，适合只应
从固定网站或 CDN 发布媒体的 feed；留空则接受任意公网媒体主机。

只有 feed 确实会发布本地或私有网络媒体，并且本服务应当访问这些地址时，才设置
`allow_private_media = true`。它会允许非公网媒体地址；同时仍可使用
`allowed_media_hosts` 限定可接受的主机名。修改任一字段后运行 `sources validate`。

## 手工精调和命令行

Panel 适合常用操作；需要修改完整字段或批量调整时，直接编辑目录文件：

```bash
asmr-tg-backup sources path \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources export \
  --config ~/.config/asmr-tg-backup/config.toml \
  --output sources.backup.toml
# 编辑 sources.toml
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources apply \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources list \
  --config ~/.config/asmr-tg-backup/config.toml
```

CLI 在没有 `--config` 时会查找当前目录的 `config.toml`。使用其他主配置时替换上面的
路径。也可以先编辑另一份文件，再用下面的命令校验并原子替换当前目录：

```bash
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml \
  --file ./candidate.toml
asmr-tg-backup sources apply \
  --config ~/.config/asmr-tg-backup/config.toml \
  --file ./candidate.toml
```

`apply` 会在一个事务中更新 SQLite 镜像；目录中被删除的来源也会从可轮询来源集合中
移除，已有媒体和任务历史仍保留。升级旧安装时运行一次
`asmr-tg-backup sources migrate --config ~/.config/asmr-tg-backup/config.toml`，会从已有
SQLite 来源或旧版来源声明创建目录。

旧版 `[[origins]]`、`[[channels]]` 和 `[[feeds]]` 只在 `sources.toml` 尚不存在时参与一次
迁移：当前旧声明会替代 SQLite 中过期的 config 来源，再与已有的 Panel/catalog 来源
安全合并；如果 ID 或远端身份冲突，迁移会停止并明确报错，不会暗中选择某一方优先。
`sources.toml` 一旦存在，旧声明会被忽略并产生启动警告。确认目录内容后应从
`config.toml` 删除这些声明，此后运行期只有一份来源配置真源。

常用来源字段：

| 字段 | 作用 |
| --- | --- |
| `id` | 本地稳定标识；重命名显示名称时无需修改 |
| `provider` | 内置值包括 `youtube`、`twitch` 和手工配置的 `rss`；启用的扩展可以注册 `niconico` 等其他值 |
| `kind` | YouTube 使用 `uploads` 或 `vod_after_live`；Twitch 支持 `vods`、`highlights`、`uploads`；RSS 使用 `feed`；扩展定义自己的 kind |
| `name` | Panel 和状态输出中的显示名称 |
| `external_id` | 远端标识：YouTube 频道 ID、Twitch 主播 ID/登录名、RSS feed URL，或扩展提供方定义的值 |
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
不会替换完整的视频备份文件。

完整备份文件、缩略图、投递文件和直播分段都在应用数据目录中。修改目录前请阅读
[运行与维护](../operations.md)。

## 本地文件自动保留策略 {#automatic-local-retention}

新生成的 setup 配置会在直播合并并验证完成一天后清理录制分段，即使 Telegram 投递
被阻塞也不会无限保留；Telegram 上传派生文件则在成功投递一天后清理。完整备份文件
继续保留：

```toml
[storage]
process_retention_hours = 24
backup_retention_hours = 0
archive_dir = ""
archive_after_delivery_hours = 24
archive_require_mount = true
```

`process_retention_hours` 同时控制两类过程文件：未投递直播的分段从合并完成开始计时，
Telegram 上传派生文件从成功投递开始计时。`backup_retention_hours` 控制完整备份文件。
任一字段设为 `0` 都表示一直保留对应文件。

如果要把完整备份文件转移到挂载磁盘，把 `archive_dir` 设为磁盘中已经存在的目录。
文件会在 `archive_after_delivery_hours` 到期后移动；挂载存储保持
`archive_require_mount = true` 即可，目录可以位于挂载点的下级。

SQLite 记录已完成的投递和每份备份当前所在的位置，媒体内容仍以普通文件保存在配置的
存储中。Docker 部署时，把挂载目录作为 bind mount 暴露给容器并填写容器内路径。修改
这些设置后重启服务。
