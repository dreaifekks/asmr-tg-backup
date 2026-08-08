# PyPI 与原生 Linux

PyPI 是组件最少的部署方式：一个应用进程、SQLite 和一个私有 MTProto session。
默认路径不需要运行本地 Bot API 服务。

## 安装系统工具

Debian 或 Ubuntu 可以运行：

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg curl pipx
pipx ensurepath
```

安装官方发行包：

```bash
pipx install asmr-tg-backup
asmr-tg-backup --version
```

如果经常上传大文件，可以用下面的可选 extra 命令代替上面的普通安装命令：

```bash
pipx install "asmr-tg-backup[performance]"
```

`cryptg` 只是性能加速器；不安装也可以使用核心功能。

## 运行引导式初始化

```bash
asmr-tg-backup setup
```

默认选项是 **MTProto 直接上传**。使用官方发行包时，setup 会询问：

1. BotFather token；
2. 目标 chat ID 或 `@channel`；
3. 允许打开控制面板的 Telegram 用户 ID。

它会在 `~/.config/asmr-tg-backup/` 下写入权限为 `0600` 的配置，在
`~/.local/share/asmr-tg-backup/` 下初始化 SQLite，并输出准确的运行命令。
setup 不会发送测试消息。首次运行会在数据目录创建 MTProto session；请像保护 bot
token 一样保护这个 session。

## 源码构建与自己的 Telegram application

源码构建使用 MTProto 时，需要自己的 Telegram application ID/hash。按照 Telegram
[application 创建说明](https://core.telegram.org/api/obtaining_api_id)取得一对凭据，
并在 setup 及之后每次服务运行前成对导出：

```bash
export ASMR_TG_MTPROTO_API_ID=123456
export ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
asmr-tg-backup setup
```

这两个值是不可拆分的一对；只设置其中一个会报错。也可以把两个值都写入私有的
`[telegram.mtproto]`。不要把 bot token 或 `.session` 文件复制到源码目录。

## 运行与验证

使用 setup 输出的路径，例如：

```bash
asmr-tg-backup run \
  --config ~/.config/asmr-tg-backup/config.toml
```

在另一个终端检查：

```bash
asmr-tg-backup status \
  --config ~/.config/asmr-tg-backup/config.toml
```

然后向 bot 发送 `/panel`，添加一个来源。确认一次下载和一次投递后，再继续增加来源。

## 注册用户服务

如果进程需要源码构建凭据或 Twitch 凭据，请创建私有环境文件：

```dotenv
ASMR_TG_MTPROTO_API_ID=123456
ASMR_TG_MTPROTO_API_HASH=0123456789abcdef0123456789abcdef
```

将其保存为 `~/.config/asmr-tg-backup/env` 并设置为 `0600`，然后让
`systemd --user` unit 同时引用该文件和 setup 生成的配置。`ExecStart` 应使用
`command -v asmr-tg-backup` 返回的绝对路径。

```ini
[Unit]
Description=ASMR archive and Telegram delivery worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/asmr-tg-backup run --config %h/.config/asmr-tg-backup/config.toml
EnvironmentFile=-%h/.config/asmr-tg-backup/env
Restart=always
RestartSec=10s
UMask=0077

[Install]
WantedBy=default.target
```

将其保存为 `~/.config/systemd/user/asmr-tg-backup.service`。如果
`command -v` 返回其他路径，请修改 `ExecStart`。

```bash
systemctl --user daemon-reload
systemctl --user enable --now asmr-tg-backup.service
journalctl --user -u asmr-tg-backup.service -f
```

## 高级 Bot API 初始化

在 setup 第一级菜单中选择 **Bot API**，下一层可以：

- 使用已有可信 URL；
- 验证预装的 `telegram-bot-api` 可执行文件，并在 `127.0.0.1:18081` 注册本地
  用户 unit；
- 使用 `api.telegram.org`、49 MB 安全阈值和可播放音频分块。

wheel 不会下载 C++ 服务端。选择本地服务前，应先按照 Telegram
[官方源码说明](https://github.com/tdlib/telegram-bot-api#installation)完成构建安装。
本地服务需要自己的 `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`；它与应用 MTProto
使用的 `ASMR_TG_MTPROTO_API_ID/HASH` 是两套独立配置。

setup 不会把 bot 从云端 Bot API 自动迁移到本地服务。首次在本地服务使用该 token
前，请执行 Telegram 的
[本地服务迁移流程](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server)。

## 更新

```bash
pipx upgrade asmr-tg-backup
systemctl --user restart asmr-tg-backup.service
```

更新前请备份配置、SQLite 数据库、下载文件和 MTProto session。详情参阅
[运行与维护](../operations.md)。
