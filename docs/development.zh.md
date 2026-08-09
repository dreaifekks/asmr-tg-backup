# 架构与开发

本页面向希望理解、修改或参与项目开发的人。安装和运行步骤仍以用户指南为准。

## 运行流程

```text
Panel / 来源 CLI
  -> 原子替换 sources.toml
  -> 事务同步 SQLite 来源运行镜像
内置 / 扩展 provider
  -> 使用该镜像与类型化 registry 执行来源发现
  -> SQLite 媒体与任务状态
  -> yt-dlp / ffmpeg 下载产物
  -> MTProto 或 Bot API 投递
  -> Telegram 投递记录
```

系统会先记录发现的媒体，再将工作加入队列。下载与 Telegram 投递是两个独立的持久化
任务，因此投递失败不会重新执行已经完成的下载。worker 通过租约领取任务。如果
Telegram 返回结果不明确，任务会进入 `uncertain`，不会自动重试，因为消息可能已经
被 Telegram 接收。

## 模块划分

| 模块 | 职责 |
| --- | --- |
| `cli.py`、`setup.py` | 命令、引导式初始化和私密配置生成 |
| `config.py` | TOML 读取、环境变量覆盖和校验 |
| `extension_api.py`、`extensions.py` | 稳定契约、entry-point 加载、类型化能力 registry 与 lifecycle |
| `network.py` | 任务级 route lease 与统一 HTTP/进程连接行为 |
| `source_catalog.py` | 来源目录校验、原子写入与 SQLite 同步 |
| `sources.py`、`youtube.py` | 内置 provider 发现与统一媒体元数据 |
| `service.py` | 轮询、worker 编排、重试和优雅停止 |
| `store.py` | SQLite schema、迁移、任务、租约和已跟踪资源 |
| `downloader.py` | `yt-dlp`/`ffmpeg` 执行和衍生媒体文件 |
| `telegram_mtproto.py`、`telegram.py` | MTProto 与 Bot API 媒体投递 |
| `control.py` | 带授权的 Telegram 控制面板和已跟踪文件操作 |

`config.toml` 提供进程级全局设置和来源目录路径。应用写入和数据库同步都经过
`SourceCatalogManager`：先锁定目录并原子替换 `sources.toml`，再用一个事务同步数据库；
手工修改则通过它的 `apply` 路径生效。同步失败时会恢复原来的目录内容。SQLite 副本
只用于运行，不是另一套配置权威。

## 本地开发环境

项目支持 Python 3.11 及以上版本。测试媒体流程时还需要安装 `ffmpeg` 和 `ffprobe`。

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
```

需要实际测试 MTProto 的源码构建必须提供一组完整的自有 Telegram application 配置。
单元测试必须使用 mock 与临时文件，不得连接真实来源、bot、目标地址或私密数据目录。

需要长期运行源码 checkout 时，先根据
[源码配置示例](https://github.com/dreaifekks/asmr-tg-backup/blob/master/deploy/source-config.example.toml){ target="_blank" rel="noopener noreferrer" }
创建私密配置，再直接注册当前虚拟环境：

```bash
.venv/bin/asmr-tg-backup service install \
  --config ~/.config/asmr-tg-backup/config.toml
```

生成的 unit 会记录当前虚拟环境解释器和配置路径。PyPI 安装使用同一条命令，参见
[PyPI 用户服务说明](getting-started/pypi.md#run-as-a-user-service)。

## 校验修改

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/mkdocs build --strict --clean
```

在本地预览双语文档：

```bash
.venv/bin/mkdocs serve --dev-addr 127.0.0.1:8001
```

英文页面使用 `page.md`，简体中文页面使用 `page.zh.md`。用户可见行为发生变化时，
两种语言的页面应保持结构一致。

## 必须保持的边界

- 每组数据库和 MTProto session 只能由一个应用进程使用。
- 发现、下载和投递状态必须能在重启后继续使用。
- `sources.toml` 是来源和过滤器的用户配置真源；SQLite 只保留运行镜像与状态。
- 扩展不得拥有 SQLite、任务或投递状态；进程只导入配置中显式启用的扩展 ID。
- 每个请求、子进程、上传或 client connection 必须固定一条 route，只能在安全的
  重试/重连边界切换。
- 不得自动重试 `uncertain` 状态的 Telegram 投递。
- token、application 凭据、Twitch 凭据、私密配置、数据库、下载文件和 session 不得
  进入日志、命令行参数、测试数据、源码构建包或提交记录。
- 本地资源删除必须由用户显式开启，并且只能处理下载根目录下、SQLite 精确记录的
  普通文件。

提交修改前请阅读[参与贡献](contributing.md)中的完整流程和检查清单。
