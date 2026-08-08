# 运行与维护

## CLI 工作流程

所有命令都接受 `--config`，该选项可以放在子命令之前或之后。

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

使用 `enqueue` 可以加入一个明确的 YouTube URL：

```bash
asmr-tg-backup enqueue --config config.toml \
  https://www.youtube.com/watch?v=VIDEO_ID
```

## 状态与备份

状态保存在 `[app].data_dir` 下，也可以由环境变量
`ASMR_TG_BACKUP_DATA_DIR` 覆盖。其中包括：

- `state.db` 和按版本生成的迁移备份；
- 各提供方的下载文件以及 Telegram 衍生文件；
- yt-dlp archive 文件；
- 使用过 MTProto transport 后生成的 `.session` 文件。

对数据目录进行文件系统级备份前，应先停止应用。私密配置和环境变量文件需要单独备份，
并使用能够安全保存密钥的目标位置。session 带有可复用的 bot 授权，因此备份位置必须
具备与 bot token 相同的访问控制。不要把 Bot API 卷当作应用归档；它是独立服务卷。

## 更新 Compose 安装

先备份数据，然后拉取并重建官方应用镜像：

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
pipx upgrade asmr-tg-backup
asmr-tg-backup --version
systemctl --user restart asmr-tg-backup.service
systemctl --user status asmr-tg-backup.service
```

如果执行了 schema 迁移，请保留对应的 `state.db.bak-*` 文件，直到确认服务、任务数量
和近期 artifacts 均正常。

## 优雅停止

SIGTERM 会停止领取新任务，并允许 worker 清空手头工作。Twitch 直播录制会先中断
ffmpeg，使当前分段能够完成封装。进程托管程序应在强制终止子进程前提供有限但充足的
宽限时间。
