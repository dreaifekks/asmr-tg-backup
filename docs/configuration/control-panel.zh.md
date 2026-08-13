# Telegram 控制面板

Telegram 控制面板把日常来源和本地资源操作集中在一条内联消息中。启用后发送
`/panel` 或 `/start` 即可打开。

## 可以完成哪些操作

通过授权的用户可以在 Panel 中：

- 添加 YouTube 和 Twitch 来源；
- 添加已启用扩展注册的提供方，例如 Niconico；
- 启用、停用、查看和移除来源；
- 为 Twitch 选择直播录制或结束后下载；
- 管理全局来源过滤器，并查看轮询或任务状态；
- 浏览已跟踪的本地文件，并按需开启经过确认的磁盘删除。

启用来源扩展并重启服务后，Panel 会增加对应的提供方按钮。需要创建来源并开始轮询时，
选择该按钮并提交所需标识。

## 使用前需要准备什么

先确定可以操作 Panel 的用户、聊天和消息主题 ID，再写入对应白名单：

```toml
[control]
enabled = true
api_base = ""
poll_interval_seconds = 10
panel_idle_timeout_seconds = 3600
delete_webhook_on_startup = true
allow_disk_delete = false
allowed_user_ids = ["123456789"]
allowed_chat_ids = []
allowed_message_thread_ids = []
```

所有非空白名单使用 AND 关系；同时配置用户和聊天时，两者都必须匹配。所有白名单都
为空时会拒绝全部命令。建议至少填写一个用户 ID；只限制聊天会允许该聊天的所有成员。

面板默认空闲一小时后关闭。设置 `panel_idle_timeout_seconds = 0` 可关闭空闲超时。

Panel 通过 Bot API long polling 接收更新。如果 bot 可能还保留 webhook，保持
`delete_webhook_on_startup = true`；服务启动时会移除该 webhook，并保留待处理更新。

## 控制面专用 Bot API 地址

`control.api_base` 决定 `getUpdates`、callback 确认、命令注册以及面板消息发送/编辑所用的
Bot API 地址。留空或省略时，与 `telegram.bot_api.api_base` 使用同一个地址。

如果媒体保持使用 MTProto 投递，而控制面板希望使用可信的本地 Bot API 服务：

```toml
[telegram]
upload_transport = "mtproto"

[telegram.bot_api]
api_base = "https://api.telegram.org"

[control]
enabled = true
api_base = "http://127.0.0.1:18081"
```

该地址会收到 bot token。远程服务必须使用 HTTPS；未加密 HTTP 只适合可信的 loopback
地址。loopback 请求始终强制直连，不会被扩展或环境代理接管。

如果 `telegram.upload_transport = "bot_api"`，通常应让
`telegram.bot_api.api_base` 与控制面板指向同一个本地服务。每个 bot token 同一时间只
分配给一个 Bot API 位置。

在 Telegram 云端 Bot API 与本地服务之间迁移 token 时，先准备本地服务并停止应用，
按照服务文档完成 bot 迁移，再修改配置并重启。迁移期间让云端与本地的 `getUpdates`
consumer 保持互斥。

## 打开控制面板

1. 保存 `[control]` 配置，并按照对应的[部署方式](../operations.md)重启服务。
2. 从白名单允许的用户、聊天和消息主题发送 `/panel` 或 `/start`。
3. 需要获取新的运行快照时选择 `🔄 刷新`；每次操作完成后 Panel 也会更新。
4. 需要一条新的 Panel 消息时，再次发送 `/panel`。

## 来源管理

Panel 按钮可以：

- 添加、启用、停用和移除 YouTube、Twitch 及已启用扩展的来源；
- 为 Twitch VOD 来源选择直播录制或归档下载；
- 查看、设置、关闭或重置全局来源过滤器；
- 查看来源轮询错误与媒体/任务统计。

每个已启用的扩展提供方都会获得自动生成的 `➕ 提供方`按钮。选择后提交
`<external_id> [显示名称]`；标识中包含空格时用引号包住。输入被接受后，新来源会在
`📚 来源`中显示对应的 `provider/kind`。RSS 保持为手工目录来源，配置方式见
[来源与下载](sources.md#rss-feed)。

使用 `/origin rename <origin_id> <name>` 可以重命名来源；使用
`/origin history <origin_id>` 可以把导入范围从 `latest` 改成 `all`。这些命令与按钮
操作的是同一份目录文件。

每次来源或过滤器变更都会先安全写入 `[sources].path` 指向的 `sources.toml`，再同步
SQLite 运行时镜像。手工编辑同一文件并执行 `asmr-tg-backup sources apply` 后，Panel
也会显示新值。不存在“Panel 配置”和“TOML 配置”之间的优先级竞争。

需要批量修改、调整完整字段或保留配置快照时，请使用[来源目录与命令行](sources.md)。

## 哪些内容保存在哪里

这里的 `<config-stem>` 表示去掉末尾 `.toml` 后的主配置文件名。

| 内容 | 保存位置 | 如何修改 |
| --- | --- | --- |
| 来源、启用状态、名称、导入范围、Twitch 模式、全局过滤器 | `sources.toml`；SQLite 仅保留同步后的运行镜像 | Panel 按钮或 `/origin` 命令；也可编辑文件后运行 `sources apply` |
| Telegram、下载、Twitch 凭据引用、Panel 地址与权限 | `config.toml` 与可选 `env` | 编辑后重启服务 |
| 受管扩展的启用状态、required 标记和私密配置引用 | `<config-stem>.extensions.toml`；默认路径是 `config.extensions.toml` | `extensions enable` 管理该 sidecar；主配置中的设置优先 |
| 受管私密扩展配置 | 主配置旁的 `extensions/<config-stem>/` | 扩展 setup 或 `extensions enable --reconfigure`；使用 `extensions doctor` 校验 |
| 轮询游标、错误、媒体、任务、投递和文件记录 | `state.db` | 由服务运行时维护 |
| 当前 Panel 消息、会话导航和 Telegram update offset | `state.db` | 由 Panel 自动维护 |
| 下载文件和 MTProto session | 应用数据目录 | 由服务维护；文件删除可在 Panel 中明确执行 |

调整来源时编辑 `sources.toml`，无需直接修改 SQLite。一份完整备份应包含
`config.toml`、`sources.toml`、可选 `env`、存在时对应的
`<config-stem>.extensions.toml` sidecar（通常为 `config.extensions.toml`）与
`extensions/<config-stem>/` 目录、保存在其他位置的全部手工 `config_file`、`state.db`、
下载文件和 MTProto session。

## 本地资源库

Panel 只列出 SQLite 中已有记录的文件，不递归扫描下载目录，也不根据文件名推断归属。

永久删除磁盘文件需要在 `config.toml` 中明确开启：

```toml
[control]
allow_disk_delete = true
```

修改后重启服务。即使已启用，删除仍要求通过授权、使用当前有效 Panel，并针对具体资源
二次确认。只有已配置受管存储根目录下被精确跟踪的普通文件可以删除，其中也包括启用的
挂载式 `[storage].archive_dir`。数据库历史与已有 Telegram 消息会保留；归档挂载不可用
时会显示为不安全或缺失资源，不会因此改删其他路径。

若某个 Telegram 投递因响应边界不明确而进入 `uncertain`，对应资源详情会显示“处理
不确定投递”。操作员必须先在目标会话核实结果，再二次确认“已送达”或明确承担重复消息
风险后“强制重新发送”。两种操作都有状态版本校验和独立审计记录；Panel 不会自动重发。

`allow_disk_delete` 只控制从 Panel 发起的删除。自动清理与挂载存储使用
`[storage].process_retention_hours`、`[storage].backup_retention_hours` 和
`archive_dir`。参见
[本地文件自动保留策略](sources.md#automatic-local-retention)。
