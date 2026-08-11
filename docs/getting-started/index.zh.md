# 选择部署方式

PyPI 和 Docker Compose 运行的是同一个应用，也使用同一套 TOML 配置。先选择你习惯的
安装方式；Telegram 上传方式之后可以单独调整。

| | PyPI / 原生 Linux | Docker Compose |
| --- | --- | --- |
| 适合场景 | 在一台 Linux 主机上轻量运行 | 用容器统一管理服务 |
| 后台运行 | `systemd --user` | Compose 重启策略 |
| 来源配置 | `~/.config/asmr-tg-backup/sources.toml` | `./settings/sources.toml` |
| 运行状态 | XDG 数据目录 | 挂载到 `/data` 的 `asmr-data` 卷 |
| 默认上传 | MTProto | MTProto |
| 媒体工具 | 自行安装 `ffmpeg`/`ffprobe` | 镜像已经包含 |
| 可选扩展 | 在核心所在的 pipx/virtualenv 环境中启用受信扩展 | 构建派生镜像，再在挂载的配置中启用 ID |

## Telegram 快捷入口 {#telegram-shortcuts}

<div class="grid cards" markdown>

-   **创建 bot**

    [打开 BotFather](https://t.me/BotFather){ target="_blank" rel="noopener noreferrer" }

    创建 bot 并复制 token，然后把 bot 加入目标频道，授予发布消息的权限。

-   **查看自己的用户 ID**

    [打开 @userinfobot](https://t.me/userinfobot){ target="_blank" rel="noopener noreferrer" }

    复制返回的数字 ID，用于授权 Telegram 控制面板。

-   **申请 Telegram API ID/hash**

    [打开 Telegram API 管理页面](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" }

    源码构建需要自有 MTProto API ID/hash；运行本地 Bot API 时也会用到。官方 PyPI 和
    GHCR 安装可以直接使用 MTProto。

</div>

另外准备好目标 chat ID；如果频道有公开用户名，也可以直接填写 `@channel`。

MTProto 会以 bot 身份登录，并在首次投递时创建可复用的 session；无需个人账号或手机
验证码。

## 选择安装方式

- [PyPI 与原生 Linux](pypi.md) 路径最短：安装软件包、运行
  `asmr-tg-backup setup`，再通过 `asmr-tg-backup service install` 注册用户服务。
- [Docker Compose](docker-compose.md) 把应用和数据放进统一的容器服务栈。

先完成核心安装和基础配置。原生安装随后可以通过
`asmr-tg-backup extensions enable <slug>` 启用受信扩展；Compose 安装则把扩展包加入
派生镜像，并在挂载的 `config.toml` 中启用对应 ID。首次连接本身就依赖代理时，在启动
服务前准备好 `proxy-router`。[扩展指南](../configuration/extensions.md)包含两种流程。

## 选择上传方式

| 方式 | 适合场景 |
| --- | --- |
| MTProto | 默认方式，直接上传文件，不需要单独运行 Bot API 服务。 |
| 已有 Bot API URL | 连接你已经在运行的 Bot API 地址。 |
| 本地 Bot API | 通过原生 systemd 或 Compose 的 `local-api` profile 运行。 |
| Telegram 云端 Bot API | 单文件按 49 MB 配置，超出后把音频拆成可独立播放的分段。 |

## setup 完成后

PyPI 的 setup 会询问 bot token、目标地址和控制面板用户 ID，并创建 `config.toml`、
`sources.toml` 与 SQLite 数据库。启动服务后，向 bot 发送 `/panel`，添加一个 YouTube
或 Twitch 来源。启用来源扩展后，发送新的 `/panel`，再使用自动生成的 provider 按钮。
Panel 会把来源和过滤器写入可编辑的 `sources.toml`，SQLite 只保存同步镜像和运行状态。
添加 Twitch 前还需要按照
[Twitch 配置说明](../configuration/sources.md#twitch-credentials)准备应用凭据。

Compose 不运行交互式 setup，而是把相同信息写入 `.env`、`config.toml` 和
`settings/sources.toml`。

源码构建通过 MTProto 上传时，需要自己的完整 API ID/hash。源码开发流程见
[架构与开发](../development.md)。
