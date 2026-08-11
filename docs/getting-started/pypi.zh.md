# PyPI 与原生 Linux

这种方式只运行一个应用进程，状态保存在 SQLite，MTProto bot session 保存在本地，
不需要另外部署 Bot API 服务。

## 1. 安装

Debian 或 Ubuntu 可以直接运行：

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg pipx
pipx ensurepath
export PATH="$HOME/.local/bin:$PATH"
pipx install "asmr-tg-backup[performance]"
asmr-tg-backup --version
```

`[performance]` 会一并安装 `cryptg`，用于加快 MTProto 上传。

## 2. 准备 Telegram

- [通过 BotFather 创建或管理 bot](https://t.me/BotFather){ target="_blank" rel="noopener noreferrer" }
- [通过 @userinfobot 查看自己的数字用户 ID](https://t.me/userinfobot){ target="_blank" rel="noopener noreferrer" }
- 准备目标 chat ID；频道有公开用户名时也可以使用 `@channel`。

然后运行：

```bash
asmr-tg-backup setup
```

运行 setup 后按提示填写 bot token、目标地址和控制面板用户 ID；没有特殊需求时，上传
方式选择默认的 **MTProto 直接上传**。setup 随后会创建：

- `~/.config/asmr-tg-backup/config.toml`
- `~/.config/asmr-tg-backup/sources.toml`
- `~/.local/share/asmr-tg-backup/state.db`

MTProto 登录的是 bot，不是个人 Telegram 账号。第一份媒体真正投递时，session 文件才会
出现在数据目录中。

## 3. 按需配置 Twitch 凭据

YouTube 不需要额外的来源凭据。准备添加 Twitch 来源时，把 Twitch 应用凭据
写入用户服务读取的环境文件：

```dotenv
TWITCH_CLIENT_ID=replace-with-client-id
TWITCH_CLIENT_SECRET=replace-with-client-secret
```

```bash
chmod 600 ~/.config/asmr-tg-backup/env
```

[Twitch 配置说明](../configuration/sources.md#twitch-credentials)提供开发者控制台入口，
也说明了已有 access token 的用法。

## 4. 注册后台服务 {#run-as-a-user-service}

下面一条命令会按照当前 pipx/虚拟环境和配置路径生成 systemd 用户服务，同时完成注册、
立即启动和开机自启动：

```bash
asmr-tg-backup service install
```

这条命令可以重复执行：它会更新本应用生成的 unit 并重启 worker，所以 `pipx` 升级后
再运行一次即可。

使用非默认配置时，在后面加上 `--config /absolute/path/config.toml`。查看状态和实时日志：

```bash
systemctl --user status asmr-tg-backup.service
journalctl --user -u asmr-tg-backup.service -f
```

以后需要停止并注销服务时运行：

```bash
asmr-tg-backup service uninstall
```

注销只移除用户服务，不会删除配置、环境文件、SQLite、下载文件或 MTProto session。
用户级 linger 也会保留，因为其他用户服务可能仍在使用它。

如果只想在前台临时运行：

```bash
asmr-tg-backup run \
  --config ~/.config/asmr-tg-backup/config.toml
```

## 5. 添加来源

向 bot 发送 `/panel`，添加一个 YouTube 或 Twitch 来源。Panel 会更新
`~/.config/asmr-tg-backup/sources.toml` 并同步运行时数据库。需要批量或完整字段调整时，
可以编辑该文件后运行：

```bash
asmr-tg-backup sources validate \
  --config ~/.config/asmr-tg-backup/config.toml
asmr-tg-backup sources apply \
  --config ~/.config/asmr-tg-backup/config.toml
```

通过 Panel 或 `asmr-tg-backup status` 确认首次下载和投递。

## 6. 更新

更新过程会保留现有配置和数据，并按照升级后的 pipx 环境重新生成用户服务。复制备份前
先停止应用，以及备份范围内的本地 Bot API 服务，使各项状态保持一致。

完整备份 `~/.config/asmr-tg-backup/` 和 `~/.local/share/asmr-tg-backup/`。配置备份
包含 `config.toml`、`sources.toml`、可选的 `env`，以及使用一键扩展 setup 后生成的
`config.extensions.toml` 和 `extensions/config/` 下的私密文件。

部署使用原生本地 Bot API 时，还要备份
`~/.config/systemd/user/asmr-tg-backup-telegram-bot-api.service`。复制数据前停止两个
服务：

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

如果 `extensions doctor` 报告扩展缺失或版本不兼容，受信目录中的扩展可以重新运行
`extensions enable <slug>`；第三方扩展则在同一个 pipx 环境中按照其安装说明更新。
再次完成两项校验后再继续。

部署使用本地 Bot API 时先启动该服务，再刷新应用 unit 并启动 worker：

```bash
systemctl --user start asmr-tg-backup-telegram-bot-api.service  # 仅本地 Bot API 部署
asmr-tg-backup service install
systemctl --user status asmr-tg-backup.service
```

## 其他上传与构建方式

- [Telegram 投递](../configuration/telegram.md) 介绍自定义/本地 Bot API 和云端音频分块。
- [架构与开发](../development.md) 介绍源码构建。源码构建需要自备 API ID/hash，可在
  [Telegram API 管理页面](https://my.telegram.org/apps){ target="_blank" rel="noopener noreferrer" }申请。
