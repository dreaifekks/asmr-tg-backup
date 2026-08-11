# 参与贡献

欢迎改进应用功能、受支持的部署方式，以及面向用户或开发者的文档。

## 开发环境

项目支持 Python 3.11 及以上版本。

```bash
git clone https://github.com/dreaifekks/asmr-tg-backup.git
cd asmr-tg-backup
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[docs]"
```

修改媒体处理流程时需要安装 `ffmpeg` 和 `ffprobe`。源码构建只有在实际测试 MTProto
上传时才需要自有 Telegram API ID/hash；普通单元测试不会使用真实的来源或 Telegram
凭据。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests -v
```

测试应使用临时数据目录，并对来源服务、Telegram 和媒体进程边界进行 mock。每项行为
修改都应添加对应测试；涉及失败恢复时，也需要覆盖回滚路径。

## 文档

用户可见行为需要同时维护英文和简体中文文档。MkDocs i18n 插件会把 `page.md` 与
`page.zh.md` 配对。

```bash
.venv/bin/mkdocs build --strict --clean
.venv/bin/mkdocs serve --dev-addr 127.0.0.1:8001
```

README 用作项目入口；详细安装、运行和故障排查说明放在 MkDocs，具体实现约束放在
[架构与开发](development.md)页面。

## 项目约束

- 内容发现、下载和 Telegram 投递状态保存在 SQLite 中。
- 下载与投递任务分别维护重试和租约。
- 结果不明确的 Telegram 发送会进入 `uncertain`，不会自动重试。
- 同一数据库与 MTProto session 只由一个应用进程使用。
- `sources.toml` 是来源和过滤器的配置真源；Panel 与 CLI 都必须通过来源目录管理器修改，
  SQLite 只保留同步镜像和运行状态。
- 扩展负责注册能力，SQLite、任务、下载、投递状态和来源目录仍由核心统一管理；
  进程只导入当前配置显式启用的扩展 ID。
- 每次请求、子进程、上传或 client connection 在完成前固定使用同一条网络 route；
  只在对应的重试或重连边界选择新 route。
- token、API 凭据、私密配置、数据库、下载文件和 session 不进入日志、命令行参数、
  测试数据、构建产物或提交记录。
- 本地资源删除保持为可选功能，只处理下载根目录下由 SQLite 精确记录的文件。

## 提交修改前

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -m unittest discover -s tests
.venv/bin/mkdocs build --strict --clean
```

用户可见行为发生变化时，在 `CHANGELOG.md` 的 `Unreleased` 中补充记录。
