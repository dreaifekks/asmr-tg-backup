# 运行与维护

## CLI 工作流程

运行类命令都接受 `--config`，该选项可以放在子命令之前或之后。

```bash
asmr-tg-backup init --config config.toml
asmr-tg-backup status --config config.toml
asmr-tg-backup poll --config config.toml --once --no-process
asmr-tg-backup process --config config.toml
asmr-tg-backup run --config config.toml
```

- `init` 创建数据目录和 SQLite schema，不执行轮询。
- `status` 输出任务数量和近期条目。
- `poll --no-process` 只发现并排队，不下载。
- `process` 不获取来源，只处理已排队任务。
- `run` 启动持续来源轮询、worker、与提供方无关的直播轮询和可选控制循环。

使用 `enqueue` 可以单独加入一个 YouTube URL：

```bash
asmr-tg-backup enqueue --config config.toml \
  https://www.youtube.com/watch?v=VIDEO_ID
```

原生后台服务可以直接注册或注销：

```bash
asmr-tg-backup service install
asmr-tg-backup service uninstall
```

注销命令只移除生成的 systemd unit，配置和数据目录中的内容都会保留。

来源和过滤器可以优先从 `/panel` 修改。需要手工精调时先校验再应用：

```bash
asmr-tg-backup sources path --config config.toml
asmr-tg-backup sources validate --config config.toml
asmr-tg-backup sources apply --config config.toml
asmr-tg-backup sources list --config config.toml
```

`sources apply` 只同步运行时数据库镜像，不会改写 `config.toml`；修改 `config.toml`
中的全局设置后仍需重启服务。

## 状态与备份

状态保存在 `[app].data_dir` 下，也可以由环境变量
`ASMR_TG_BACKUP_DATA_DIR` 覆盖。其中包括：

- `state.db` 和按版本生成的迁移备份；
- 各提供方的下载文件以及 Telegram 衍生文件；
- yt-dlp archive 文件；
- 使用过 MTProto transport 后生成的 `.session` 文件。

`sources.toml` 通常位于配置目录而不是数据目录。这里的 `<config-stem>` 表示去掉末尾
`.toml` 后的主配置文件名。完整备份包含：

- 主配置、来源目录和可选的 `env`；
- 名为 `<config-stem>.extensions.toml` 的托管扩展 sidecar；
- `extensions/<config-stem>/` 下的 setup 私密文件，以及手工配置到其他位置的
  `config_file`；
- 上述数据目录内容。

部署使用原生本地 Bot API 服务时，还要保存
`~/.config/asmr-tg-backup/telegram-bot-api.env`、
`~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service`，以及应用数据根目录下的
`telegram-bot-api` 目录。

使用默认 `config.toml` 时，托管文件分别是 `config.extensions.toml` 和
`extensions/config/`。这些文件可能包含代理订阅地址或其他凭据，因此备份位置应采用
与 bot token、MTProto session 相同的访问控制。

也可以单独执行
`asmr-tg-backup sources export --config /path/to/config.toml --output sources.backup.toml`
导出来源目录快照。对数据目录进行文件系统级备份前先停止应用，使 SQLite、下载文件和
session 处于同一份快照。复制本地 Bot API 数据前，也要停止对应的原生或 Compose 服务。
Compose Bot API 卷属于独立服务卷；部署依赖它时请单独备份。

## 更新 Compose 安装

更新会替换应用镜像，并保留配置和持久化卷。先停止应用，再备份 `.env`、
`config.toml`、`./settings/`、数据卷、挂载到容器内的扩展配置及其 Compose override，
以及派生镜像使用的 Dockerfile 或固定版本清单。服务栈包含 `local-api` profile 时，
同时停止该服务，并把 Bot API 卷纳入快照。

拉取并校验官方应用镜像，再重新创建容器：

```bash
docker compose stop asmr-tg-backup
docker compose --profile local-api stop telegram-bot-api  # 仅 local-api 部署
docker compose pull asmr-tg-backup
docker compose run --rm asmr-tg-backup \
  extensions doctor --config /config/config.toml
docker compose run --rm asmr-tg-backup \
  sources validate --config /config/config.toml
```

校验通过后，只启动应用，或者恢复完整的本地 Bot API profile：

```bash
# 仅应用：
docker compose up -d asmr-tg-backup

# 应用与本地 Bot API profile：
docker compose --profile local-api up -d

docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

正常更新期间保留命名卷；`docker compose down -v` 会删除这些卷。

使用源码构建时，在两项校验前运行 `docker compose build --pull asmr-tg-backup`。启用扩展
的部署则更新派生镜像 `FROM` 中的核心版本，继续固定扩展版本，重新构建派生镜像，并让
`ASMR_TG_BACKUP_IMAGE` 指向新标签。重建服务前，对这个镜像执行相同的
`extensions doctor` 和 `sources validate`。

## 更新 PyPI 安装

这套更新会保留现有配置和数据，并根据升级后的可执行文件刷新 systemd unit。先停止应用；
部署使用原生本地 Bot API 时，同时停止该服务。在两个服务都已停止的状态下完成文件系统
级备份：

```bash
systemctl --user stop asmr-tg-backup.service
systemctl --user stop asmr-tg-backup-telegram-bot-api.service  # 仅本地 Bot API 部署
```

保持服务停止，升级并完成校验：

```bash
pipx upgrade asmr-tg-backup
asmr-tg-backup --version
asmr-tg-backup extensions doctor \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml
```

启动服务前先处理扩展兼容性或来源校验错误。对于受信目录中的扩展，重新运行
`extensions enable <slug>`，即可安装升级后核心所选择的版本；第三方扩展则在同一个
pipx 环境中按照其安装说明更新。随后重新执行两项校验。

部署使用本地 Bot API 时先启动该服务，再刷新并启动应用服务：

```bash
systemctl --user start asmr-tg-backup-telegram-bot-api.service  # 仅本地 Bot API 部署
asmr-tg-backup service install
systemctl --user status asmr-tg-backup.service
```

`service install` 会重新生成 unit 并启动 worker。

如果执行了数据库结构迁移，请保留对应的 `state.db.bak-*` 文件，直到确认服务、任务
数量和近期文件均正常。

## 停止服务

SIGTERM 会停止领取新任务，并允许 worker 清空手头工作。直播录制会先中断 ffmpeg，
使当前分段能够完成封装。进程托管程序应在强制终止子进程前提供有限但充足的宽限时间。
