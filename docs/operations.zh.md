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
- `run` 启动持续来源轮询、worker、Twitch 直播轮询和可选控制循环。

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

`sources.toml` 通常位于配置目录而不是数据目录。完整备份应包含 `config.toml`、
`sources.toml`、可选 `env` 和上述数据目录内容。也可以单独执行
`asmr-tg-backup sources export --output sources.backup.toml` 导出来源目录快照。
对数据目录进行文件系统级备份前应先
停止应用，并使用能够安全保存密钥的目标位置。session 带有可复用的 bot 授权，因此
备份位置必须具备与 bot token 相同的访问控制。不要把 Bot API 卷当作应用归档；它是
独立服务卷。

## 更新 Compose 安装

先备份 `./settings/sources.toml` 与数据卷，然后拉取新镜像并重新创建容器：

```bash
docker compose pull asmr-tg-backup
docker compose up -d asmr-tg-backup
docker compose ps
docker compose logs --tail=200 asmr-tg-backup
```

如果服务栈包含内置 Bot API，请加入 `--profile local-api`。正常更新期间不要运行
`docker compose down -v`，否则会删除命名卷。明确需要源码构建时，请先运行
`docker compose build --pull asmr-tg-backup`，再重建服务。

## 更新 PyPI 安装

停止用户服务、备份数据，然后升级 `pipx` 安装：

```bash
systemctl --user stop asmr-tg-backup.service
pipx upgrade asmr-tg-backup
asmr-tg-backup --version
asmr-tg-backup service install
systemctl --user status asmr-tg-backup.service
```

如果执行了数据库结构迁移，请保留对应的 `state.db.bak-*` 文件，直到确认服务、任务
数量和近期文件均正常。

## 停止服务

SIGTERM 会停止领取新任务，并允许 worker 清空手头工作。Twitch 直播录制会先中断
ffmpeg，使当前分段能够完成封装。进程托管程序应在强制终止子进程前提供有限但充足的
宽限时间。
