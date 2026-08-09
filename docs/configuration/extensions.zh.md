# 扩展

0.5 版引入扩展 API level 1。扩展是单独安装的 Python distribution，通过
`asmr_tg_backup.extensions` entry-point group 注册能力。核心只会从自身所在的 Python
环境中发现扩展，并且只有在配置中显式启用 ID 后才会导入扩展代码。

## 核心仍然负责什么

扩展拿不到数据库连接、任务队列、downloader 或 Telegram token。以下职责仍由核心
掌握：

- `sources.toml` 校验与原子替换；
- SQLite 媒体、checkpoint、去重、租约和重试状态；
- 直播与普通 worker lane；
- yt-dlp/ffmpeg 执行和已跟踪产物；
- Telegram 投递状态，包括禁止自动重发的 `uncertain` 边界。

当前扩展可以注册来源定义或网络能力：

```text
已安装 distribution
  -> entry point -> ExtensionHost -> 类型化 registrar -> 冻结后的 runtime
                                           |-> 来源提供方定义
                                           |-> 唯一 connection policy
                                           `-> 唯一 HTTP transport

来源 candidate -> 核心 SQLite/任务 -> 核心探测/下载 -> 核心投递
route request   -> policy lease     -> 单次请求/进程/连接
```

多个扩展可以添加互不冲突的来源 provider ID。一个进程只能注册一个 connection policy
和一个 HTTP transport；如果同时启用两个竞争的网络路由器，启动会明确失败，不会让
安装顺序暗中决定行为。

## 安装到同一个环境

使用 pipx 安装核心时，把扩展注入已有应用环境：

```bash
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-niconico-origin @ git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

使用虚拟环境时，调用该环境的解释器：

```bash
.venv/bin/python -m pip install \
  'git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

官方容器保持最小依赖；需要扩展时构建一个很薄的派生镜像：

```dockerfile
FROM ghcr.io/dreaifekks/asmr-tg-backup:0.5.0
RUN python -m pip install --no-cache-dir \
    'git+https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin.git@v0.1.0'
```

请固定 tag 或不可变 commit ID。不要把未经审查的扩展安装到持有 Telegram 凭据或
私密媒体的服务中。

## 启用与检查

`extensions list` 只读取 distribution 元数据，不导入扩展代码：

```bash
asmr-tg-backup extensions list --config config.toml
```

在核心配置中启用已安装的 ID：

```toml
[extensions]
enabled = ["dreaife.niconico-origin"]

[extensions."dreaife.niconico-origin"]
required = true
request_timeout_seconds = 30
```

较大或包含秘密的配置应放在核心配置旁边的私密文件中：

```toml
[extensions]
enabled = ["dreaife.proxy-router"]

[extensions."dreaife.proxy-router"]
required = true
config_file = "proxy.toml"
```

相对 `config_file` 路径以 `config.toml` 所在目录为基准。行内字段会覆盖该文件的
顶层同名字段。两个文件都应使用 `0600` 权限。

重启前运行：

```bash
asmr-tg-backup extensions doctor --config config.toml
asmr-tg-backup sources validate --config config.toml
```

`doctor` 会导入已启用的包、检查 manifest ID 与 API level、注册能力、启动并停止扩展
lifecycle、用组合后的 provider registry 校验来源目录，并实例化来源 adapter。缺少
必需扩展或能力冲突都会导致失败。只有部署明确允许缺少某个可选能力时，才使用
`required = false`。

## 添加扩展来源

Telegram Panel 目前只提供内置 YouTube/Twitch 的引导流程。扩展特有的来源 kind 应
写入 `sources.toml`，再通过核心 CLI 应用。Niconico 扩展示例：

```toml
[[origins]]
id = "niconico-asmr-live"
provider = "niconico"
kind = "live_search"
name = "Niconico ASMR"
external_id = "ASMR"
enabled = true
bootstrap = "all"
max_results = 20
```

provider 会把当前节目转成标准化 `live_stream` candidate。核心快速轮询 lane 负责探测
和录制每个节目、持久化片段、重试中断、合并结束后的片段并投递产物。部分 Niconico
节目仍需 cookie、年龄确认、会员资格或付费权限；相关 yt-dlp 设置应放入私密的
`niconico` download profile。

## 分域连接路由

网络 policy 接收 `RouteRequest` 并返回一个 `RouteLease`。lease 会覆盖完整操作：

| Scope | Lease 边界 |
| --- | --- |
| `origin.resolve` | 一次 HTML 或 yt-dlp 来源引用解析 |
| `source.notification` | 一次直播/feed 通知请求 |
| `source.discovery` | 一次目录/OAuth 请求 |
| `media.probe` | 一个 yt-dlp 探测进程 |
| `media.download` | 一个完整 yt-dlp 进程或一次直播片段尝试 |
| `telegram.control.receive` | 一次 Bot API `getUpdates` 请求 |
| `telegram.control.send` | 一次控制 Bot API 请求 |
| `telegram.delivery.bot_api` | 一次不可幂等的 curl 上传 |
| `telegram.delivery.mtproto` | 一个持久 Telethon 连接 |

短小且幂等的 HTTP 操作在下次重试时可以获得另一条 route。核心不会在正在运行的
yt-dlp 进程中重新申请 route；Mihomo 之类的托管 gateway 可以把后来新建的分片连接
交给新选出的健康订阅节点，但无法迁移已经建立的 TCP 连接。Bot API 上传开始发送后
不会重新申请 route，不明确结果仍进入 `uncertain`。MTProto 只在断开并重建 client
connection 后改变核心 route。

回环 HTTP 端点始终直连。当 Bot API 配置为本地 `telegram-bot-api` daemon 时，扩展
只能控制核心到 daemon 的连接；daemon 自己连接 Telegram 的出口必须在其进程、容器
或 network namespace 层配置。

## 扩展作者的 API 兼容边界

Entry-point factory 返回带 `ExtensionManifest` 和 `register`、`start`、`stop` 方法的
对象：

```toml
[project.entry-points."asmr_tg_backup.extensions"]
"example.provider" = "example_extension:create_extension"
```

只使用 `ytb_tg_backup.extension_api` 中的公共类型。设置
`manifest.api_level = 1`，并在包元数据声明兼容核心范围，例如
`asmr-tg-backup>=0.5,<0.6`。注册完成后 runtime registry 会冻结；lifecycle `start`
在核心构建后运行，`stop` 按相反顺序执行。启动失败时，已经启动的扩展会被停止。

来源 adapter 只获得窄化的 `SourceAdapterContext`：带路由的 HTTP client、logger 与
扩展数据目录。运行失败应抛出带稳定 code 和可选 retry delay 的 `SourceError`。来源
选项应在 provider definition 中校验和规范化，不应读取核心内部实现。如果该 provider
的所有媒体 URL 都需要 `websocket` 之类的 route 能力，应在
`SourceProviderDefinition.route_features` 中声明；网络 policy 就能在启动 yt-dlp 前
排除不兼容端点。
