# Telegram 控制面板

Telegram 控制面板是管理来源的首选入口。发送 `/panel` 或 `/start`，即可在一个内联
消息中管理来源和过滤器、查看运行状态并浏览已跟踪的本地资源。

## 启用控制面板

```toml
[control]
enabled = true
poll_interval_seconds = 10
panel_idle_timeout_seconds = 3600
allow_disk_delete = false
allowed_user_ids = ["123456789"]
allowed_chat_ids = []
allowed_message_thread_ids = []
```

所有非空白名单使用 AND 关系；同时配置用户和聊天时，两者都必须匹配。所有白名单都
为空时会拒绝全部命令。建议至少填写一个用户 ID；只限制聊天会允许该聊天的所有成员。

面板默认空闲一小时后关闭。设置 `panel_idle_timeout_seconds = 0` 可关闭空闲超时。

## 来源管理

Panel 按钮可以：

- 添加、启用、停用和移除 YouTube/Twitch 来源；
- 为 Twitch VOD 来源选择直播录制或归档下载；
- 查看、设置、关闭或重置全局来源过滤器；
- 查看来源轮询错误与媒体/任务统计。

使用 `/origin rename <origin_id> <name>` 可以重命名来源；使用
`/origin history <origin_id>` 可以把导入范围从 `latest` 改成 `all`。这些命令与按钮
操作的是同一份目录文件。

每次来源或过滤器变更都会先安全写入 `[sources].path` 指向的 `sources.toml`，再同步
SQLite 运行时镜像。手工编辑同一文件并执行 `asmr-tg-backup sources apply` 后，Panel
也会显示新值。不存在“Panel 配置”和“TOML 配置”之间的优先级竞争。

需要批量修改、调整完整字段或保留配置快照时，请使用[来源目录与命令行](sources.md)。

## 哪些内容保存在哪里

| 内容 | 保存位置 | 如何修改 |
| --- | --- | --- |
| 来源、启用状态、名称、导入范围、Twitch 模式、全局过滤器 | `sources.toml`；SQLite 仅保留同步后的运行镜像 | Panel 按钮或 `/origin` 命令；也可编辑文件后运行 `sources apply` |
| Telegram、下载、Twitch 凭据引用、Panel 权限 | `config.toml` 与可选 `env` | 编辑后重启服务 |
| 轮询游标、错误、媒体、任务、投递和文件记录 | `state.db` | 由服务运行时维护 |
| 当前 Panel 消息、会话导航和 Telegram update offset | `state.db` | 由 Panel 自动维护 |
| 下载文件和 MTProto session | 应用数据目录 | 由服务维护；文件删除可在 Panel 中明确执行 |

因此不应直接编辑 SQLite 来改来源配置。备份时同时保存 `config.toml`、
`sources.toml`、可选 `env`、`state.db`、下载文件和 MTProto session。

## 本地资源库

Panel 只列出 SQLite 中已有记录的文件，不递归扫描下载目录，也不根据文件名推断归属。

永久删除磁盘文件需要在 `config.toml` 中明确开启：

```toml
[control]
allow_disk_delete = true
```

修改后重启服务。即使已启用，删除仍要求通过授权、使用当前有效 Panel，并针对具体资源
二次确认。只有下载根目录下被精确跟踪的普通文件可以删除；数据库历史与已有 Telegram
消息会保留。
