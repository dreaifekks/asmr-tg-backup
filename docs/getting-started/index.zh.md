# 选择部署方式

两种安装方式运行同一个 Python 应用，并使用同一套 TOML 配置。请根据更新和进程监管
方式选择安装路线；Telegram 上传 transport 可以独立切换。

| 关注点 | PyPI / 原生 Linux | Docker Compose |
| --- | --- | --- |
| 安装 | `pipx` 或虚拟环境 | 官方 GHCR 镜像或本地源码构建 |
| 进程监管 | `systemd --user` 或其他原生 supervisor | Compose 重启策略 |
| 持久状态 | XDG 数据目录 | `/data` 命名卷 |
| 默认媒体上传 | MTProto | MTProto |
| 本地 Bot API | 可选预装可执行文件与生成的用户 unit | 可选 `local-api` profile |
| 系统工具 | 自行安装 `ffmpeg` 与 `curl` | 镜像已经包含 |

## 推荐路线

希望组件最少时，使用 [PyPI 与原生 Linux](pypi.md)。官方包可以直接使用 MTProto，
因此 `asmr-tg-backup setup` 通常只需要 bot token、目标地址和控制面板用户 ID。

偏好容器服务、命名卷备份，或希望把可选 Bot API 服务放进同一服务栈时，使用
[Docker Compose](docker-compose.md)。

## 官方发行包与源码构建

官方 PyPI 与 GHCR 发行包可直接使用默认 MTProto 路径。也可以通过环境变量用自己的
完整凭据对覆盖默认值：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

两个值必须一起设置。源码构建没有发行默认值，因此选择 MTProto 时需要这组环境变量，
或在私有 `[telegram.mtproto]` 中填写对应值；也可以在 setup 中改选 Bot API。

bot token 与 MTProto session 始终属于本地安装，必须保持私密。

## 不进行旧配置兼容迁移

这一版本直接使用新的嵌套 transport 配置。如果手上有早期 checkout 生成的实验配置，
请重新运行 `asmr-tg-backup setup`，或与包内示例对照修改，不要期待旧的扁平 Telegram
字段自动迁移。
