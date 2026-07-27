# Anime Manager

一个本地运行、人工确认优先的动漫媒体刮削管理器。它不会自动移动或删除媒体文件；所有 NFO 与图片写入都需要先预览，再由用户明确确认。

## 当前能力

- 扫描本地媒体目录，使用 BLAKE2b-256 内容哈希识别改名文件。
- 从文件名解析作品标题与集号，并按目录和作品标题分组。
- 使用 AniDB 官方标题库进行本地候选搜索，确认后通过 HTTP XML API 获取详情。
- 提供带明显“模拟”标识的 DMM、Getchu 演示适配器。
- 每个来源独立选择候选，永久保存外部 ID 和字段来源。
- 生成 Jellyfin 优先的 `tvshow.nfo`、剧集 NFO 或 `movie.nfo`。
- 写入前展示差异；覆盖时自动保留时间戳备份。

## Windows 启动

要求 Python 3.12+、Node.js 20+，推荐安装 ffprobe 以读取视频参数。

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

打开 <http://127.0.0.1:5173>。停止服务：

```powershell
.\scripts\stop.ps1
```

## AniDB 配置

首次搜索会下载 AniDB 官方标题库，之后 24 小时内不会重复下载。标题搜索不需要账号；确认 AniDB 候选并获取详情前，需要在“设置”中填写 AniDB 已注册的 HTTP API `client` 和 `clientver`。

项目不抓取 AniDB 网页，并把详情请求限制为至少 2 秒一次、同一 AID 当日复用缓存。

## 开发验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests
Set-Location frontend
npm test
npm run build
```

数据库迁移文件位于 `backend/alembic`，默认数据文件是 `backend/data/anime.db`。

