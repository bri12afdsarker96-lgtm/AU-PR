# AU&PR 0.8-B 素材索引、选片可信度与试片验收 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 在现有“五列表格 → 素材候选 → 成片”试验链路上，建立可增量更新的只读素材索引，排除不可播放或明显不合适的候选，并以三份真实试片证明技术成片和选片质量各自达到可验收水平。

**Architecture:** 沿用 `storyboard.py` 的表格解析和可人工锁定剪辑方案；新增位于软件数据目录的 SQLite 索引，以及独立的候选召回模块。素材目录始终只读，索引和抽帧只写软件数据/任务输出目录。保留旧 `flat` 等模式，并在真实素材有变动时明确标出失效镜头，不静默复用。Codex 订阅接入留到本地选片链路稳定后的 0.9-A。

**Tech Stack:** Python 3.11 标准库、SQLite、现有 FFprobe/FFmpeg、`unittest`、现有本机 Web UI。

---

## 当前基线和本轮边界

- 工作树：`D:\GitHub\By\AU&PR\AU-PR-v071-20260815`，分支 `codex/v071-20260815`，基点 `1ef5bc0`。原仓 `D:\GitHub\By\AU&PR\AU-PR` 的脏状态不碰。
- 上一轮已在本工作树实现五列导入、文本匹配、人工选候选/入点、源片入点渲染、Web 入口和复核 CSV；这些改动尚未提交，不把它们当作远端已有功能。`storyboard.py` 目前每次 `rglob` 扫库，候选仅凭路径文本，`confidence=text_only` 且全需复核。没有 SQLite、视频健康缓存、视觉核对、真实试片验收或可用的 Codex 桥接。
- 旧蓝图：`.hermes/plans/2026-10-07_212102-aupr-spreadsheet-to-film-codex-blueprint.md`。本计划仅细化下一增量，不重新设计产品。旧 Obsidian 项目笔记停留在 2026-07，不能替代当前工作树事实。
- 输入样本：`D:\桌面\2026-09-09\2026-09-09` 的 145 份五列表、1,849 镜头；真实素材根 `D:\宇宙素材` 可能正在另一电脑/任务整理。只读扫描，绝不改名、移动、覆盖或删除源素材。旧台账 `D:\桌面\协同作战标.xlsx` 仅作可选 BJ 编号描述补充，不按行号或口播全文直接映射。
- 已有 43 项定向测试通过，但上一轮更广的 64 项测试出现 6 项未归因失败/错误：CLI GBK 输出、两个旧 Web frame-lock、两个 FFmpeg 缺失预检、进度条 181/180。开始新功能前先判定它们是历史问题还是新回归；不得在计划里宣称全量通过。
- 本轮不做 Codex 登录、付费发行、145 部批量渲染或全库视觉嵌入。这些留在后续关口。当前阶段保持“低置信度可先成片，但显著提示人工复核”的产品口径。

## 执行约定

以下命令均从 `D:\GitHub\By\AU&PR\AU-PR-v071-20260815` 的 PowerShell 运行。每个有代码的任务按“新增失败测试 → 运行确认红灯 → 最小实现 → 定向测试绿灯 → 只暂存本任务文件并提交”执行。先保护/核查已有未提交改动；不得用 `reset --hard`、`checkout --` 或整仓 `git add .`。测试素材只建在临时目录；真实库仅做只读预检。

```powershell
$env:PYTHONPATH='source;tests'
$env:PYTHONIOENCODING='utf-8'
$py='D:\Python\Python311\python.exe'
```

### Task 0: 封存当前状态并确认回归边界

**Objective:** 在改动素材检索前，知道哪些失败已存在，并为现有 0.8-A 垂直切片建立可追溯检查点。

**Files:** 只检查当前 `git diff`、`git status`、`tests/test_dub_align_storyboard.py`、`tests/test_dub_align_web.py`、`tests/test_dub_align_source_trim.py`；不在该步骤改代码。

1. 运行 `git status --short`、`git diff --check`，核对变动只属于上一轮表格成片功能和蓝图。
2. 运行 `& $py -m unittest discover -s tests -p 'test_dub_align_storyboard.py'`，预期通过；再运行 `& $py -m unittest discover -s tests -p 'test_dub_align_*.py'`，记录完整失败名、异常和运行环境。不要仅靠上轮口述的“6 项”判断。
3. 对上述 6 项分别用 `git show 1ef5bc0:<path>` 或隔离的只读比较确认与当前改动关系；若是本切片造成，先补复现测试并修复；若是原有缺陷，记录在本计划执行日志/issue，禁止为了绿灯随意改断言。
4. 在现有变动测试通过并确认 diff 后，只暂存该切片涉及的文件，做单独检查点提交；不推送、合并或触碰原仓。以后每任务只提交本任务文件。

### Task 1: 素材目录索引的最小数据契约

**Objective:** 用独立 SQLite 缓存替代每次计划都从零扫描目录，同时不把数据库写进素材库。

**Files:** Create `source/dub_align_studio/material_catalog.py`; Create `tests/test_dub_align_catalog.py`.

1. 写失败测试 `test_catalog_database_lives_outside_library`：临时 `library/BJ1_地球.mp4`、临时 `cache/catalog.sqlite3`，调用 `Catalog(db_path, library).refresh(probe=False)` 后断言数据库仅在 `cache`，库里原文件的字节/mtime 不变；`Catalog.list_assets()` 返回一个含 `asset_id`、`path`、`relative_path`、`size_bytes`、`mtime_ns` 的记录。
2. 运行 `& $py -m unittest test_dub_align_catalog.CatalogTests.test_catalog_database_lives_outside_library -v`，预期导入或方法不存在而失败。
3. 实现 `Catalog(db_path: Path, library: Path)`、`refresh(probe: bool = True) -> ScanStats`、`list_assets() -> list[Asset]`；SQLite 至少保存 `schema_version`、规范化根路径、相对路径、大小、纳秒 mtime、探测状态和媒体信息。`asset_id` 为根标识 + 规范化相对路径的稳定哈希；不能只用不唯一的 BJ 编号。数据库目录由调用方提供，库内路径必须被拒绝。
4. 同命令应通过；提交 `feat(catalog): add read-only library index schema`。

### Task 2: 增量扫描和失效标记

**Objective:** 重扫时只处理新增/变更文件，删除或重命名后的旧路径不能继续被选中。

**Files:** Modify `source/dub_align_studio/material_catalog.py`; Test `tests/test_dub_align_catalog.py`.

1. 新增 `test_refresh_marks_removed_and_skips_unchanged`：首扫 2 个视频，第二次不变时 `changed=0`，更名一个后新路径有效、旧路径 `missing`，另一个不触发媒体复探测。
2. 跑该测试确认失败；实现单次目录枚举、扩展名过滤、`_EXCLUDED_FOLDERS` 同步、文件 stat 比较和缺失标记。不要让扫描删除手工锁定方案或原文件；记录 `scan_generation`，失败扫描不能误将全库标为缺失。
3. 重跑定向测试通过；提交 `feat(catalog): refresh changed assets incrementally`。

### Task 3: 媒体健康与候选资格

**Objective:** 空文件、损坏文件、无视频轨或时长不合法的素材不进入自动候选。

**Files:** Modify `source/dub_align_studio/material_catalog.py`; Test `tests/test_dub_align_catalog.py`; reference only `source/dub_align_studio/bulk_dub/ffmpeg_pipeline.py` and `source/dub_align_studio/settings.py` for现有探测/二进制解析方式。

1. 用测试注入的 `probe(path)` 假函数分别返回可播放元数据和错误，断言 `playable`, `unreadable`, `missing` 状态、`duration_seconds/width/height/fps`；同签名文件的第二扫不得重探测，变化后必须重探测。
2. 定向运行确认失败；最小实现用现有 `settings.ffmpeg_tool('ffprobe')` 解析工具，调用 FFprobe JSON；解析超时、JSON 错误和无视频轨均变成可解释状态，不让全库扫描因一条坏文件中止。把探测器做可注入以避免单元测试依赖本机二进制。
3. 定向运行通过；增加 1 条小型真视频集成测试（只有本机可用 FFmpeg 时运行）；提交 `feat(catalog): cache video health and probe metadata`。

### Task 4: 旧台账可选描述导入

**Objective:** 增加检索证据，但绝不把旧台账当成新表的逐行答案。

**Files:** Modify `source/dub_align_studio/material_catalog.py`; Test `tests/test_dub_align_catalog.py`.

1. 用临时 XLSX 构造 M 列 BJ 编号、E/G/H 文本的 2 条记录，测试 `Catalog.import_legacy_metadata(path)` 按编号绑定；重复 BJ 编号对应多个视频时给每个候选相同描述和 `source=legacy_workbook`，缺编号则不绑定。台账不存在时扫描仍能运行。
2. 定向运行确认失败；实现导入和来源标记。复用现有 XLSX 读取能力，若现有读取器不支持任意列，则只增加最小的列读取接口，别在 catalog 重写压缩包解析。
3. 定向运行通过；提交 `feat(catalog): enrich assets by optional legacy ids`。

### Task 5: B/D/E 分项检索并消除伪匹配

**Objective:** 把“口播主题、静态画面、运动要求”分开评分，泛词/模板词不应压过具体概念。

**Files:** Create `source/dub_align_studio/material_retrieval.py`; Create `tests/test_dub_align_retrieval.py`; Modify `source/dub_align_studio/storyboard.py` only at接入边界。

1. 测试构造 3 条目录/台账描述：`BJ1_黑洞吞噬恒星`、`BJ2_星系红移`、`BJ3_宇宙背景`。镜头 B=“黑洞吞噬恒星”，D=“恒星被拉伸”，E=“镜头推进”，断言 BJ1 首位；把 D 改为“红移星系”时，B/D 分项证据都可见而非一个无解释总分。仅命中“宇宙/素材/视频/画面”等泛词时应 `review_required=True` 且不能宣称高置信。
2. 定向运行确认失败；实现 `retrieve(shot, assets, limit=...) -> list[Candidate]`。沿用现有 n-gram，但对 B/D/E 各自保留命中词、分值、来源；排除模板前缀、BJ 编号和过于常见的库级词，排序稳定且不遍历每镜头的全目录。候选理由必须能在人机界面解释。
3. 定向运行通过；提交 `feat(retrieval): rank storyboard candidates with evidence`。

### Task 6: 缺片、时长与画幅约束

**Objective:** 候选必须可播放且适配时长/画幅；无候选时明确停下，而不是随机选择第 N 个文件。

**Files:** Modify `source/dub_align_studio/material_retrieval.py`; Test `tests/test_dub_align_retrieval.py`; Modify `source/dub_align_studio/storyboard.py`.

1. 新增测试：最佳文本文件已损坏时选次佳可播放项；源片比表格时长短时保留可重复/减速策略标记但降低分值；画幅严重冲突时降权；全为坏文件时返回 `no_playable_candidate`，`prepare_plan` 不落一个随机路径。已有人工锁定的失效源必须提示重新选择。
2. 定向运行确认失败；实现过滤和降权。`confidence` 暂只允许 `text_only` / `unverified` 等保守等级；未看片不得标“已确认”。异常状态逐镜头写到复核报告。
3. 定向运行通过；提交 `fix(retrieval): reject unavailable sources and report gaps`。

### Task 7: 接入索引并兼容既有剪辑方案

**Objective:** `prepare_plan` 使用 catalog/retrieval，同时保留既有 JSON、人工选择和入点数据，旧模式不受影响。

**Files:** Modify `source/dub_align_studio/storyboard.py`; Modify `source/dub_align_studio/studio_pipeline.py`; Modify `tests/test_dub_align_storyboard.py`; Create/modify `tests/test_dub_align_edit_plan.py` if extraction to `edit_plan.py` is justified.

1. 写迁移测试：读取当前 `schema_version=1` 的真实形状方案；同一 Excel/素材不变保留已锁定 `source` 和 `source_in_seconds`；一行文案改变只使这一行重新检索；源路径搬走时该行变成 `needs_review`，不得静默改到同 BJ 其他文件；索引数据库变化但素材实质不变时不重复配音。
2. 定向运行确认失败；做最小的 `schema_version=2` 兼容读取/保存及 per-row 变更判断。目录索引默认放 `settings.data_root()/缓存/素材索引/<library-hash>.sqlite3`，并允许测试传临时路径。计划仍写在每部作品输出目录，保留 `row`/`sheet_name`/`candidates`/`source_in_seconds` 字段供现有 UI/渲染读取。
3. 运行 `test_dub_align_storyboard.py`、`test_dub_align_web.py`、`test_dub_align_source_trim.py`；通过后提交 `feat(storyboard): use catalog without losing manual edits`。

### Task 8: 页面显示真实索引/选片状态

**Objective:** 用户知道扫描进度、失效数、每个推荐为何入选和哪些镜头必须人工复核。

**Files:** Modify `source/dub_align_studio/web_server.py`; Modify `source/dub_align_studio/web/index.html`; Test `tests/test_dub_align_web.py`.

1. 接口测试断言 `/api/storyboard/plan` 响应含 `indexed_count`、`playable_count`、`unreadable_count`、`missing_count`、逐候选 `score_breakdown/evidence`、逐镜头 `review_reason`；对已有 v1 JSON 仍能返回可用预览。HTML 标记测试断言有“仅文本匹配/待复核”的明显提示。
2. 定向运行确认失败；复用现有任务状态/队列，不另建后台框架；扫描不可在请求线程无限阻塞，应可见进度或先给“正在索引”状态。页面只显示相对目录/编号，具体本机路径仅在本机详情中展示。
3. 定向运行通过；提交 `feat(web): explain candidate confidence and catalog status`。

### Task 9: 技术质检产物

**Objective:** MP4 存在不等于成片成功；结束任务前必须可探测音视频轨、时长和镜头数量。

**Files:** Modify `source/dub_align_studio/studio_pipeline.py`; Modify `source/dub_align_studio/web_server.py`; Create `source/dub_align_studio/film_qa.py`; Create `tests/test_dub_align_film_qa.py`.

1. 使用测试媒体或探测器注入写失败测试：无视频轨、无音轨、时长偏差超过容差、计划 2 镜头但只产 1 片段均产生明确失败代码；通过时写 `技术质检.json`，含成片实际秒数、音视频流、每镜头源文件/入点/音频秒数。
2. 定向运行确认失败；实现质检并在任务完成状态前调用。容差先依据现有渲染帧率/拼接策略确定，不能硬编码“0 秒误差”；保留失败中间产物供排障，不覆盖上一份合格成片。
3. 定向运行通过，再运行 FFmpeg 集成测试；提交 `feat(qa): verify rendered film before success`。

### Task 10: 三份真实试片与人工语义门槛

**Objective:** 分别证明“技术上能自动成片”与“选片语义上可用”。

**Files:** No source file required unless this task暴露可复现缺陷；测试夹具/结果写任务输出目录，不写 `D:\宇宙素材` 或原 XLSX。建议新增 `tests/test_dub_align_real_storyboard_smoke.py` 作为可选、显式启用的本机 smoke test，避免普通 CI 依赖 D 盘素材。

1. 对“时间旅行”(12)、 “星系红移”(14)、 “一毫米黑洞”(13) 共 39 镜头生成候选与可复核清单，记录输入表哈希、素材索引时间、缺片/坏片/需复核数。先用小型模拟配音验证渲染结构，再用当前已保存的配音方案各生成真实 MP4；不把测试语音误当正式成片。
2. 每部执行 FFprobe 技术检查：可解码、音视频轨、时长、字幕/镜头段数量、源片入点在源片范围内；抽看片头/中段/片尾。若用户的正式声音设置不可用，技术 smoke 仍可进行，但正式片验收保持未完成。
3. 生成 39 行人工评审表，至少分“画面主体正确、运动吻合、无明显错误题材、可用/需替换”，并保留具体源编号和理由。没有人工评审结果，不宣称自动选片质量通过；只可称“候选覆盖 39/39”。
4. 对暴露问题先做失败复现再改检索，比较修改前后的 39 行结果。验收门槛：3 部正式 MP4 均通过技术质检，39 行无悄悄缺片/坏片，语义可用率由人工评审给出并达成用户接受的阈值；若阈值未确定，报告原始评分，不自定“已达标”。

## 统一验证和下一阶段关口

开发完成时运行：

```powershell
& $py -m unittest discover -s tests -p 'test_dub_align_catalog.py'
& $py -m unittest discover -s tests -p 'test_dub_align_retrieval.py'
& $py -m unittest discover -s tests -p 'test_dub_align_storyboard.py'
& $py -m unittest discover -s tests -p 'test_dub_align_web.py'
& $py -m unittest discover -s tests -p 'test_dub_align_source_trim.py'
& $py -m unittest discover -s tests -p 'test_dub_align_film_qa.py'
& $py -m unittest discover -s tests -p 'test_dub_align_*.py'
git diff --check
git status --short
```

输出一张本轮结果表：测试通过/失败（注明原有或新回归）、145 表/1,849 行解析情况、库扫描总数/可播放数/失效数、39 镜头候选覆盖、三片技术质检、39 行人工语义评分。真实库统计必须标明采集时间，因为素材仍在变动。

0.8-B/C 达标后再开始 0.9-A Codex 订阅原型：用官方授权的本机 App Server，先做文本 + 少量候选抽帧的结构化建议与降级测试；不传整库/完整视频，不读取或复用桌面 Codex 私有凭据，不以 API Key 冒充订阅，AI 决定仍须受候选集合和本地技术校验约束。若计划收费分发，先重新核实当时官方订阅集成资格与条件；本轮不把该能力写进发行承诺。

## 风险与应对

- `D:\宇宙素材` 变化导致路径失效：catalog 将旧路径标 `missing`，方案显示复核；不按相同 BJ 编号自动替换人工锁定素材。
- 全库 FFprobe 成本高：仅新增/变更文件探测，可中断/续扫；扫描进度和坏文件数可见。首扫耗时需实测，不给虚构 ETA。
- 文本匹配无法证明画面正确：证据和置信度保持保守；39 镜头人工评分是下一轮是否引入关键帧/视觉重排的决策依据。
- 现有更广测试未全绿：先归因再修，不能以新功能测试通过掩盖旧回归。
- 订阅可用范围会变化：Codex 集成放在独立原型闸门，实施时查官方最新文档并做真实登录/调用验证。

本文件为下一增量的实施计划；写计划时未修改软件代码、原表、素材库、提交或远端。
