# 扩展

核心服务已经包含 YouTube、Twitch、下载与 Telegram 投递的完整流程。当部署需要
其他来源提供方或按任务类型分配网络路由时，再选择对应扩展：

- 启用 `niconico-origin`，在 Telegram Panel 中增加 Niconico provider 按钮；
- 启用 `proxy-router`，分别选择通知、发现、探测、下载或 Telegram 连接中
  需要使用代理的 scope；
- 内置来源与直连已经满足需求时，保持扩展未启用即可。

扩展是单独安装的 Python distribution，通过
`asmr_tg_backup.extensions` entry-point group 注册能力，并运行在核心所在的同一个
Python 环境中。核心只会导入当前配置已启用的扩展 ID。0.5 版引入 runtime 扩展
API level 1，0.6 版增加受信的一键 setup 层。

## 核心仍然负责什么

数据库连接、任务队列、downloader 和 Telegram token 始终由核心持有。核心统一负责：

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
和一个 HTTP transport。同时启用两个网络路由器时，启动校验会报告能力冲突。

## 一条命令启用

两个参考实现分别是
[`proxy-router`](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router)
和
[`niconico-origin`](https://github.com/dreaifekks/asmr-tg-backup-ext-niconico-origin)
扩展。每个部署只需安装自己需要的能力。对于核心内置受信目录中的扩展，正常路径只需
一条命令：

```bash
asmr-tg-backup extensions enable proxy-router
asmr-tg-backup extensions enable niconico-origin
```

`enable` 会精确匹配核心随包发布的受信短名；第三方包使用下文的手工安装路径。
随后它会把固定版本安装到当前 pipx/虚拟环境，先静态检查 runtime 与 setup entry point，
再运行扩展自己提供的最小配置、写入启用状态、执行与 `doctor` 相同的组合运行时检查；只有当前正在
运行的核心托管 systemd 服务使用同一份主配置时，才会安全重启。重复启用一个健康
扩展不会重复安装、写文件或重启；`--reconfigure` 可重新配置，`--no-restart` 可保留
当前进程。

代理向导默认使用 `127.0.0.1:7891` SOCKS5 和“仅媒体探测/下载”预设，也支持现有
HTTP/SOCKS URL、隐藏输入的 Mihomo 订阅和更广的 scope 预设。Niconico 扩展本身无需
配置；命令会显示可选的 ASMR 直播搜索建议，由你决定是否把它添加为来源。

启用的扩展注册非内置来源 provider 后，Telegram 面板会自动增加对应按钮，例如
`➕ Niconico`。点击后输入 `<来源标识> [显示名称]`；标识包含空格时需使用引号包住。
希望创建来源并开始轮询时，再提交这段输入。创建后，该来源会在 `📚 来源` 中显示为
`provider/kind`。启用扩展并重启服务后，发送新的 `/panel`，或者刷新当前仍有效的
Panel，即可重新生成 provider 按钮。

该命令保持主配置不变。`<config-stem>` 表示去掉末尾 `.toml` 后的主配置文件名。
命令会原子维护：

- 与主配置同目录的 `<config-stem>.extensions.toml`，保存已启用 ID 和受管设置；
- 与主配置同目录的 `extensions/<config-stem>/<filename>`，保存私密扩展设置。

默认主配置 `config.toml` 对应 `config.extensions.toml` 与
`extensions/config/<filename>`。受管文件权限为 `0600`，主配置中的显式设置优先。
如果 setup、校验或服务重启失败，命令会恢复之前的受管状态与私密设置；已安装的包
可以直接用于下次启用。

## 原生手工安装

原生部署优先使用上面的一键启用命令。由配置管理系统负责安装包时，把选中的扩展
注入核心所在的同一个环境。

使用 pipx 时：

```bash
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-proxy-router==0.2.0'
pipx inject asmr-tg-backup \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

使用虚拟环境时，调用该环境的解释器：

```bash
.venv/bin/python -m pip install \
  'asmr-tg-backup-ext-proxy-router==0.2.0' \
  'asmr-tg-backup-ext-niconico-origin==0.2.0'
```

采用这种由包管理系统维护的方式时，继续完成
[手工启用与检查](#manual-enable-and-validation)。

## 在 Docker Compose 中安装扩展 {#docker-extensions}

Docker 部署包含两个持久层：

1. 派生镜像保存扩展 Python 包；
2. 宿主机挂载的配置负责选择扩展 ID，并提供私密设置。

这样每次重建容器都会得到同一套扩展运行环境。在现有容器的 shell 中临时执行
`pip install` 只会改变那个容器，Compose 替换容器后这次安装就会消失。

目前两个受信扩展对应的包名、运行时 ID 与私密文件如下：

| 能力 | 镜像中安装的包 | `config.toml` 中启用的 ID | 私密文件 |
| --- | --- | --- | --- |
| 代理选路 | `asmr-tg-backup-ext-proxy-router==0.2.0` | `dreaife.proxy-router` | 需要 |
| Niconico 来源 | `asmr-tg-backup-ext-niconico-origin==0.2.0` | `dreaife.niconico-origin` | 不需要 |

### 1. 创建派生镜像

在 Compose 项目目录创建 `Dockerfile.extensions`：

```dockerfile
ARG CORE_VERSION=0.6.5
FROM ghcr.io/dreaifekks/asmr-tg-backup:${CORE_VERSION}

ARG PROXY_ROUTER_VERSION=0.2.0
ARG NICONICO_ORIGIN_VERSION=0.2.0

RUN python -m pip install --no-cache-dir \
    "asmr-tg-backup-ext-proxy-router==${PROXY_ROUTER_VERSION}" \
    "asmr-tg-backup-ext-niconico-origin==${NICONICO_ORIGIN_VERSION}"
```

只保留当前部署需要的包。用不可变的本地标签构建镜像，让更新与回滚都有明确目标：

```bash
docker build --pull \
  --build-arg CORE_VERSION=0.6.5 \
  --build-arg PROXY_ROUTER_VERSION=0.2.0 \
  --build-arg NICONICO_ORIGIN_VERSION=0.2.0 \
  -f Dockerfile.extensions \
  -t asmr-tg-backup:0.6.5-extensions .
```

在 `.env` 中选择这个镜像：

```dotenv
ASMR_TG_BACKUP_IMAGE=asmr-tg-backup:0.6.5-extensions
```

### 2. 启用已安装的 ID

编辑宿主机 `config.toml` 中已有的 `[extensions]` 区块。下面的例子同时启用两个包：

```toml
[extensions]
enabled = [
  "dreaife.proxy-router",
  "dreaife.niconico-origin",
]

[extensions."dreaife.proxy-router"]
required = true
config_file = "extensions/proxy-router.toml"

[extensions."dreaife.niconico-origin"]
required = true
```

只保留镜像中实际安装的 ID。Dockerfile 中填写包名，`config.toml` 中填写运行时 ID。

### 3. 按需创建代理设置

Niconico 不需要私密扩展文件，只安装 Niconico 时可以跳过这一步。使用代理选路时，
在宿主机创建目录和私密文件：

```bash
mkdir -p extensions
chmod 700 extensions
# 用下方设置创建 extensions/proxy-router.toml。
chmod 600 extensions/proxy-router.toml
```

仅让媒体探测与下载使用 HTTP/SOCKS 代理的例子如下：

```toml
fail_closed = true
routes = [
  "http://host.docker.internal:7890",
  "socks5h://host.docker.internal:7891",
]

[scopes]
"media.probe" = "proxy"
"media.download" = "proxy"
```

应用容器中的 `127.0.0.1` 指向容器自身。`compose.yaml` 已把
`host.docker.internal` 映射到 Docker 宿主机，但宿主机上的代理还需要监听 Docker
网桥能够访问的地址；把监听范围限制在受信的本机或 Docker 网络。代理作为另一个
Compose 服务运行时，直接使用它的服务名。订阅设置和完整 scope 列表见
[`proxy-router` 扩展说明](https://github.com/dreaifekks/asmr-tg-backup-ext-proxy-router)。

### 4. 挂载私密设置

创建 `compose.extensions.yaml`：

```yaml
services:
  asmr-tg-backup:
    volumes:
      - ./extensions:/config/extensions:ro
```

把 override 加入 `.env`，之后每条 Compose 命令都会使用这个挂载：

```dotenv
COMPOSE_FILE=compose.yaml:compose.extensions.yaml
```

如果 `COMPOSE_FILE` 已经包含其他 override，在原值末尾追加
`:compose.extensions.yaml`，不要覆盖原值。只使用 Niconico 时无需私密文件，也可以
省略这个 override。

### 5. 校验并启动

先确认 Compose 解析到了派生镜像，再检查并校验组合后的扩展运行环境：

```bash
docker compose config --images
docker compose run --rm asmr-tg-backup \
  extensions list --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

`extensions list` 应把每个选中的 ID 标记为已安装且已启用。三项检查都通过后，启动
刚刚构建的同一个镜像：

```bash
docker compose up -d --no-build asmr-tg-backup
docker compose logs --tail=100 asmr-tg-backup
```

使用 Niconico 时，启动后发送新的 `/panel` 或刷新当前有效的 Panel，确认出现
`➕ Niconico`。使用代理选路时，在不打印私密地址或订阅的前提下，从服务日志确认
选中的 policy 已生效。

### 更新或回滚

更新时修改核心与扩展版本参数，构建一个新镜像标签，让
`ASMR_TG_BACKUP_IMAGE` 指向新标签，并在重建前重新运行上面的三条校验命令。确认新版
服务正常前保留旧标签；需要回滚时，把 `.env` 切回旧标签，再用 `--no-build` 重建应用。

## 手工启用与检查 {#manual-enable-and-validation}

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

这种手工配置仍适用于第三方扩展或完全由配置管理系统维护的部署。重启前运行：

```bash
asmr-tg-backup extensions doctor --config config.toml
asmr-tg-backup sources validate --config config.toml
```

`doctor` 会导入已启用的包、检查 manifest ID 与 API level、注册能力、启动并停止扩展
lifecycle、用组合后的 provider registry 校验来源目录，并实例化来源 adapter。缺少
必需扩展或能力冲突都会导致失败。只有部署明确允许缺少某个可选能力时，才使用
`required = false`。

## 添加扩展来源

Telegram Panel 会发现已启用的扩展 provider，并通过自动生成的按钮添加其默认来源
kind。等价的手工方式是把扩展特有的来源写入 `sources.toml`，再通过核心 CLI 应用。
Niconico 扩展示例：

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
`manifest.api_level = 1`，并在包元数据声明兼容核心范围。仅使用 runtime API level 1
的扩展可以支持 0.5；使用 setup API level 1 的扩展应要求
`asmr-tg-backup>=0.6,<0.7`。注册完成后 runtime registry 会冻结；lifecycle `start`
在核心构建后运行，`stop` 按相反顺序执行。启动失败时，已经启动的扩展会被停止。

来源 adapter 只获得窄化的 `SourceAdapterContext`：带路由的 HTTP client、logger 与
扩展数据目录。运行失败应抛出带稳定 code 和可选 retry delay 的 `SourceError`。来源
选项应在 provider definition 中校验和规范化，不应读取核心内部实现。如果该 provider
的所有媒体 URL 都需要 `websocket` 之类的 route 能力，应在
`SourceProviderDefinition.route_features` 中声明；网络 policy 就能在启动 yt-dlp 前
排除不兼容端点。

提供方可选实现 `SourceProviderDefinition.resolve_media_url`：接收一个 URL，返回
一个 `MediaCandidate`。provider、content_kind、external_id 和元数据必须与正常发现
保持一致，才能复用下载和投递去重。校验域名和具体视频路径，去掉合集参数，返回规范的
单视频 URL；有效订阅 URL 返回 `None`，无效或不支持的输入抛出 `ValueError`。
解析器不能订阅来源、遍历合集或下载媒体。返回普通视频/归档候选，不要返回依赖启用来源
的频道直播录制候选。核心负责持久入队，并自动在 Panel 显示单视频输入提示。该字段默认
为 `None`，旧扩展无需修改注册代码；此时显式 `url` 请求会提示暂不支持。

扩展也可以使用相同 ID 发布独立 setup entry point，把安装交互留在扩展仓库而不是
runtime 对象中：

```toml
[project.entry-points."asmr_tg_backup.extension_setups"]
"example.provider" = "example_extension.setup:create_configurator"
```

Setup API level 1 只暴露提示适配器、已有私密配置、由核心序列化为 TOML 的 mapping
结果，以及可选来源建议。核心统一负责写文件、enable/doctor/restart 与失败回滚；扩展
setup 代码拿不到数据库、Telegram token 或服务控制权。
