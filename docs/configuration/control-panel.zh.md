# Telegram 控制面板

可选的控制 bot 提供单消息内联面板，用于管理来源、统计、过滤器和已跟踪的本地资源。
发送 `/panel` 或 `/start` 即可打开。

## 安全启用

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

所有非空白名单之间使用 AND 关系。如果同时配置了用户和聊天，则两者都必须匹配。
如果全部白名单为空，所有命令都会被拒绝。建议至少配置一个允许的用户 ID；只配置聊天
白名单会允许该聊天中的所有成员操作。

面板默认在空闲一小时后关闭。只有确实需要面板无限期保持活动时，才把
`panel_idle_timeout_seconds` 设置为 `0`。

## 来源管理

面板可以：

- 添加、启用、禁用和移除由 bot 管理的 YouTube 与 Twitch 来源；
- 为 Twitch 频道选择直播或 VOD 模式；
- 查看来源轮询错误以及媒体和任务统计；
- 查看、设置、禁用或重置全局来源过滤器。

通过配置文件管理的来源可以查看，但只能读取。请修改对应 TOML 项并重启服务。

## 本地资源库

面板列出 SQLite artifacts 表中记录的文件，不会递归扫描下载目录，也不会根据文件名
推断归属。

永久删除磁盘文件需要明确开启：

```toml
[control]
allow_disk_delete = true
```

修改后需重启。即使启用删除，操作仍要求通过授权、使用当前有效的面板，并对具体资源
二次确认。只有配置下载根目录下、被精确跟踪的普通文件才可删除。数据库历史和已有的
Telegram 消息会保留。
