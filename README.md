# Anime Manager

一个本地运行、人工确认优先的动漫媒体刮削管理器。它不会自动移动或删除媒体文件；所有 NFO 与图片写入都需要先预览，再由用户明确确认。

## 当前能力

- 扫描本地媒体目录，使用 BLAKE2b-256 内容哈希识别改名文件。
- 从文件名解析作品标题与集号；同一子目录内尚未绑定的文件自动合并为一个待确认分组。
- 使用 AniDB 官方标题库进行本地候选搜索，支持英文、日文、罗马字等别名匹配，候选统一优先显示日文标题。
- 确认候选后通过 AniDB HTTP XML API 获取详情，作品主标题按日文、AniDB 主标题、英文的顺序回退。
- 通过 DMM/FANZA Web Service API 搜索动画商品并读取真实元数据。
- 通过 Getchu 官方站内检索和商品详情页搜索动画商品并读取真实元数据。
- 每个来源独立选择候选，永久保存外部 ID 和字段来源。
- 已绑定作品支持按“缺少简介”筛选，并可搜索其他已启用来源、人工确认候选后仅补充缺失简介。
- 支持将抓取简介自动翻译为简体中文；选择 OpenAI 兼容服务时按 OpenAI、腾讯云 TMT、原简介的顺序回退。
- 生成 Jellyfin 优先的 `tvshow.nfo`、剧集 NFO 或 `movie.nfo`。
- 写入前展示差异；覆盖时自动保留时间戳备份。
- 可将待确认分组直接添加到已有作品，不重复创建作品或 AniDB 映射。
- 支持预览后将作品视频批量移动到日文作品目录，并按 `作品名 - SxxExx - 集标题.ext` 重命名；目标冲突时禁止执行。
- 支持在作品页搜索番名或文件名、播放媒体文件，以及解除作品与媒体文件的绑定而不删除物理文件。

## 数据流

```mermaid
flowchart LR
    subgraph LOCAL["本地媒体"]
        FS[媒体目录]
        SCAN["扫描、解析与分组"]
    end

    subgraph SOURCE["外部元数据"]
        SOURCES[AniDB / DMM / Getchu]
    end

    subgraph CORE["匹配与目录"]
        REVIEW[候选匹配与人工确认]
        CATALOG[(作品目录)]
    end

    subgraph OUTPUT["预览与输出"]
        PREVIEW[重命名 / NFO / 图片预览]
        ARTIFACTS[媒体目录与 Jellyfin 文件]
    end

    FS -->|发现视频| SCAN
    SCAN -->|待匹配作品| REVIEW
    SOURCES -->|候选与详情| REVIEW
    REVIEW -->|确认绑定| CATALOG
    CATALOG -->|作品和分集数据| PREVIEW
    PREVIEW -->|用户确认写入| ARTIFACTS
```

主图刻意保持 7 个节点：`作品目录` 抽象了 `anime`、`media_file`、`match_group`、`source_mapping`、候选和来源快照等持久化细节；外部来源也先聚合为一个元数据边界。

`anime` 保存作品级元数据，例如标题、年份、类型和总集数；`media_file` 每行对应一个实际媒体文件，通过 `anime_id` 绑定作品，并在 `episode` 字段保存该文件的集号。总集数 `anime.episode_count` 与单文件集号 `media_file.episode` 是两个独立字段。当前可在作品详情中修改单个文件的集号。

### BLAKE2b-256 哈希缓存

文件内容哈希持久化在 `media_file` 表的 `content_hash` 字段中，`hash_algorithm` 记录为 `blake2b-256`；这份缓存属于数据库记录，不使用 `data/cache` 目录。扫描同一路径时，只有数据库中记录的 `size` 和 `modified_ns` 与当前文件完全一致且已有哈希，才直接复用缓存。缓存未命中时，扫描器按 4 MiB 分块读取完整文件并重新计算哈希。

```mermaid
flowchart TD
    START[扫描一个视频]

    subgraph LOOKUP["定位已有记录"]
        PATH["按媒体库与绝对路径查询"]
        SAME{"大小和纳秒修改时间一致，且已有哈希？"}
    end

    subgraph HASH["取得内容哈希"]
        REUSE[复用 content_hash]
        CALC["分块读取文件并计算 BLAKE2b-256"]
    end

    subgraph PERSIST["恢复绑定并持久化"]
        RENAMED{"当前路径是否没有记录？"}
        MATCH["按媒体库、哈希和 missing 状态查找旧记录"]
        SAVE["更新或新增 media_file 并提交"]
    end

    START --> PATH --> SAME
    SAME -->|是| REUSE
    SAME -->|否| CALC
    REUSE --> RENAMED
    CALC --> RENAMED
    RENAMED -->|是| MATCH --> SAVE
    RENAMED -->|否| SAVE
```

扫描开始时，同一媒体库的旧记录会先标记为 `missing`。因此文件在程序外被改名后，新路径会重新计算哈希，再通过相同的 `content_hash` 找回旧记录及其作品绑定；仅仅依靠哈希找回改名记录并不能省略这次计算。

作品详情和“全部作品”的批量重命名走另一条路径：预览阶段不修改哈希缓存；确认执行后移动文件，只更新 `media_file.path` 和 `relative_path`，保留原来的 `content_hash`、`hash_algorithm`、`size` 和 `modified_ns`。同一媒体库内的正常重命名不改变文件内容和修改时间，因此下次扫描可以直接命中原缓存；如果底层文件属性发生变化，下次扫描会自动重新计算。

“全部作品”预览和执行均使用后台任务并通过 SSE 报告进度，避免慢速 NAS 使普通 HTTP 请求超时。预览生成的精确重命名计划保存在对应 `task_record.result` 中，执行时复用该计划，不会再次扫描全部作品目录；移动前仍会统一复核源文件、目标路径、媒体库边界和数据库记录。预览后文件状态发生变化时，执行会在移动任何文件之前失败并要求重新预览。NAS 断开或个别作品目录不可访问时，该作品会作为跳过项保留原因，不会中止其他作品的预览。

```mermaid
sequenceDiagram
    actor USER as 用户
    participant API as 批量重命名接口
    participant TASK as 后台任务与 SSE
    participant FS as 文件系统
    participant DB as media_file
    participant SCAN as 扫描器

    USER->>API: 请求重命名预览
    API-->>USER: 返回预览任务 ID
    API->>TASK: 后台逐部生成计划
    TASK->>DB: 读取 present 文件和现有路径
    TASK->>FS: 检查目录、关联文件和目标冲突
    TASK-->>USER: SSE 进度与完整预览计划
    Note over DB: 预览不修改哈希缓存

    USER->>API: 携预览任务 ID 确认执行
    API-->>USER: 返回执行任务 ID
    API->>TASK: 复用缓存计划并重新校验
    TASK->>FS: 移动视频及关联文件
    TASK->>DB: 更新 path 和 relative_path
    Note over DB: content_hash、算法、大小和修改时间保持不变
    alt 全部移动和数据库提交成功
        DB-->>TASK: 提交成功
        TASK-->>USER: SSE 完成事件与新路径
    else 移动或提交失败
        TASK->>DB: 回滚事务
        TASK->>FS: 按相反顺序移回文件
        TASK-->>USER: SSE 失败事件
    end

    USER->>SCAN: 下次扫描
    SCAN->>FS: 读取新路径的大小和修改时间
    SCAN->>DB: 查询新路径的缓存记录
    alt 大小和修改时间一致
        DB-->>SCAN: 复用 content_hash
    else 文件属性变化
        SCAN->>FS: 读取完整文件并重新计算
        SCAN->>DB: 刷新哈希和文件属性
    end
```

### 添加新集与批量重命名

扫描新视频后，可在“待确认”页面的“添加到已绑定作品”区域搜索现有作品并直接加入。该操作只设置分组和视频的 `anime_id`，不会重复创建作品，也不会覆盖已有 AniDB 来源映射。若仍按 AniDB 候选确认，而所选 AID 已绑定到某个作品，系统也会自动复用该作品。

作品详情中的“批量重命名”会先显示完整预览。默认将视频移动到媒体库根目录下以当前作品标题命名的目录，并生成：

```text
作品名/作品名 - S01E03 - 集标题.mp4
```

文件扩展名保持不变并规范为小写。集标题来自原文件名中集号之后的文本；缺少集标题时生成 `作品名 - S01E03.ext`。缺少集号、目标文件已经存在或多个文件生成相同目标时，操作会被阻止。确认执行后，程序同步更新 `media_file.path` 和 `relative_path`；不会覆盖已有视频。

## 后端结构

后端使用 FastAPI，并按 HTTP 接口、应用编排和基础设施职责分层：

```text
backend/app/
├─ main.py                 # 创建 FastAPI 应用、注册中间件、异常处理和路由
├─ lifecycle.py            # 数据库初始化、启动清理和定时刷新任务
├─ task_events.py          # 后台任务的 SSE 事件发布与订阅
├─ queries.py              # 作品和匹配分组的公共 SQLAlchemy 查询
├─ source_settings.py      # 数据源开关及默认设置
├─ routers/                # 按 API 资源划分的 APIRouter
│  ├─ system.py            # 健康检查和仪表盘
│  ├─ libraries.py         # 媒体库及扫描任务
│  ├─ tasks.py             # 后台任务查询和进度事件
│  ├─ media.py             # 媒体文件维护和播放
│  ├─ matching.py          # 候选搜索、选择和确认
│  ├─ anime.py             # 作品、重命名及 NFO/图片输出
│  ├─ settings.py          # 应用设置
│  └─ sources.py           # AniDB 与 Getchu 来源接口
└─ services/
   └─ bulk_matching.py     # 批量搜索并确认的业务编排
```

`main.py` 保持为轻量应用入口，启动命令仍为 `app.main:app`。新增接口时，HTTP 参数、依赖注入、状态码和响应模型放在对应的 `routers` 模块；跨多个模型、外部来源或后台任务的流程放在 `services` 或现有业务模块中，避免把业务编排重新堆回入口文件。

接口重构需要保持现有 URL 和 HTTP 方法。`backend/tests/test_api_routes.py` 会对 OpenAPI 操作集合进行契约检查，防止拆分或注册路由时遗漏接口。

## Windows 启动

要求 Python 3.12+、Node.js 20+，推荐安装 ffprobe 以读取视频参数。

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

前端地址为 <http://127.0.0.1:5173>，后端地址为 <http://127.0.0.1:18010>。停止服务：

```powershell
.\scripts\stop.ps1
```

## AniDB 配置

首次搜索会下载 AniDB 官方标题库，之后 24 小时内不会重复下载。标题搜索不需要账号；确认 AniDB 候选并获取详情前，需要在“设置”中填写 AniDB 已注册的 HTTP API `client` 和 `clientver`。

项目不抓取 AniDB 网页，并把详情请求限制为至少 2 秒一次、同一 AID 当日复用缓存。

### DMM / Getchu 配置

DMM/FANZA 使用官方 Web Service API v3。需要先完成 DMM Affiliate 和 Web Service
利用注册，然后在“设置”中填写 API ID 与 API 专用 Affiliate ID。搜索范围固定为
FANZA 的数字动画楼层（`digital/anime`），不会回退到模拟数据。

Getchu 不需要账号或 API 密钥。应用使用 Getchu 官方详细搜索页的“动画 DVD”类目进行
标题检索，并在确认候选后读取商品详情。DMM 与 Getchu 的详情结果都会缓存一天。

### 简介翻译配置

在“设置 → 简介翻译”中开启自动翻译后，可配置 OpenAI Chat Completions 兼容接口或
腾讯云 TMT。目标语言固定为简体中文，源语言自动识别。

选择 OpenAI 兼容服务时，调用顺序如下：

1. OpenAI 兼容接口。
2. OpenAI 请求失败或返回拒绝说明，且 TMT 的 `SecretId`、`SecretKey` 已完整配置时，调用腾讯云 TMT。
3. TMT 也失败时保留抓取到的原简介。

翻译成功后会记录字段来源并保护译文，后续普通元数据刷新不会覆盖；翻译接口返回“无法翻译”等拒绝说明时，该文本不会被当作译文保存。

### 标题库与语言策略

AniDB 标题库中一部动画可以有多个标题。程序会将每个标题分别存入 `anidb_title` 表，并通过相同的 AniDB AID 关联：

| 字段 | 内容 |
| --- | --- |
| `aid` | AniDB 动画 ID，同一作品的标题共享该 ID |
| `title` | 原始标题文本 |
| `normalized_title` | 用于本地模糊匹配的标准化标题 |
| `language` | AniDB 语言代码，例如 `ja`、`x-jat`、`en` |
| `title_type` | 标题类型，例如 `main`、`official`、`synonym` |

搜索时，所有语言和别名都会参与相似度计算，因此仍可用英文或罗马字搜索；确定匹配的 AID 后，候选标题按以下顺序显示：

1. 日文标题（`ja`）
2. AniDB 罗马字主标题（`x-jat`）
3. 英文标题（`en`）
4. 其他语言标题

确认并获取详情后，作品主标题使用“日文官方标题 > 其他日文标题 > AniDB 主标题 > 英文标题”的回退顺序。`original_title` 同样优先保存日文标题。已有作品可在详情中点击“刷新元数据”应用当前标题策略；即使 AniDB 详情命中一天内的缓存，标题也会重新按本地标题库的语言优先级选择。

## 开发验证

```powershell
backend\.venv\Scripts\python.exe -m pytest backend\tests
Set-Location frontend
npm test
npm run build
```

数据库迁移文件位于 `backend/alembic`，默认数据文件是 `backend/data/anime.db`。
