# AU&PR 从素材索引安全关口到表格成片 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 从已完成索引安全与增量扫描的 `ecbbe46` 检查点继续，把“选一份五列工作表和现有素材库即可得到可复核成片”做成可靠的本地流程，再验证 Codex 订阅辅助剪辑。

**Architecture:** 先封闭 SQLite 缓存的素材库只读边界；随后完成增量媒体索引、带证据的检索、兼容人工锁定的剪辑计划和成片质检。Codex 只对本地筛出的少量疑难候选给建议，软件保留校验和渲染控制权。每阶段有独立可验收产物，未达门槛不声称完成。

**Tech Stack:** Python 3.11 标准库、SQLite、FFprobe/FFmpeg、现有 Web 工作台和 `unittest`；Codex 接入方式待 0.9-A 实施时以官方最新文档和真实账号验证。

---

## 当前状态（2026-10-08，媒体健康探测开发前）

- 当前工作树：`D:\GitHub\By\AU&PR\AU-PR-v071-20260815`，分支 `codex/v071-20260815`，源码检查点 `ecbbe46`。用户已要求先提交 Git 备份再继续开发；三份计划随本次备份纳入版本管理。原始 `AU-PR` 工作树不在本路线修改范围。
- 已完成原型：五列 Excel 导入、文本候选、人工选片与入点、Web 入口、试验性成片（`19cf10c`）。新索引尚未接入主流程，原型仍使用原有目录扫描和文本排名。
- P0 已完成：真正只读查询、库外缓存检查、Windows UNC 路径和真实 junction 测试；数据库缺失或路径重定向时不创建库内文件。
- P1 已完成部分：同签名资产保留缓存、变化资产重置媒体字段、扫描代次、移走/重命名标 missing、恢复待检、候选排除 missing、v1→v2 缓存升级、未知版本拒绝、跨库和归一化路径冲突拒绝。显式目录遍历传播读取/类型判断/进入前复检错误，失败扫描不改写上次索引；进入前重新检查排队目录是否变成符号链接。
- 最近一次 catalog/storyboard/source_trim 定向回归为 52 项通过，独立质量审查通过。广泛测试历史记录为 356 项中 9 项失败、2 项跳过；旧 Web/渲染/帧数及环境相关失败仍待完整归因和收口，不能据定向结果声称全套绿灯。
- 未完成：真正的 FFprobe 媒体健康与缓存、旧台账描述、可恢复探测进度、正式检索、主流程接入、三份真实试片验收和 Codex 订阅实调。最近几轮测试仅使用临时素材库。
- 两份更详细的原计划：`.hermes/plans/2026-10-07_212102-aupr-spreadsheet-to-film-codex-blueprint.md`、`.hermes/plans/2026-10-07_221250-aupr-next-sprint-catalog-retrieval-qa.md`。以下是基于实际实施状态更新后的**执行顺序**，不重复已完成的 Task 0/1。

## 后续开发路径与进入门槛

| 顺序 | 阶段 | 主要交付 | 进入下一阶段的证据 |
|---|---|---|---|
| P0 | 索引安全封口 | `list_assets` 真只读、缓存重定向拒绝、缺失 DB 不创建 | 真实或受控链接测试先失败后通过；素材库文件/目录快照不变；独立规格及质量复核通过 |
| P1 | 可用素材目录 | 增量扫描、移动/损坏标记、FFprobe 元数据缓存、可选旧台账描述 | 重扫只探测变更项；缺片/坏片不会进入自动候选；真实库只读扫描可中断/续跑 |
| P2 | 自动选片 | B 口播、D 画面、E 运动分别计分；过滤伪匹配、时长/画幅约束，返回证据 | 三份试片的 39 镜头每行有可播放候选或明确缺片原因；无随机兜底被当成语义匹配 |
| P3 | 剪辑方案与界面 | 版本化方案、人工锁定/入点保留、失效提示、索引进度与理由 | 改一行表格只重选受影响行；移走源片不错误复用；旧 `flat` 等模式无回归 |
| P4 | 可靠成片 | 音频时长对齐、源片入点、MP4 技术质检与报告 | “时间旅行”(12)、“星系红移”(14)、“一毫米黑洞”(13) 三片均有可解码 MP4，音视频轨/时长/镜头映射过关 |
| P5 | 画面质量门槛 | 39 镜头人工评审、问题复现与迭代；必要时加候选关键帧复核 | 主体/运动/题材逐行有“可用或需替换”结论与来源；质量不合格不以“成片可播放”代替 |
| P6 | Codex 订阅本机原型 | 官方许可核对、真实登录、少量候选图文的结构化建议和降级 | 当前账号真实调用成功；超限/断网/退出登录不妨碍本地流程；模型建议只能在合法候选中选择 |
| P7 | 协作剪辑与发布 | 单句自然语言修改、局部重渲染、145 表批量预检、安装包实机验证 | 锁定/撤销可追溯；批量预检可恢复；目标机器安装到成片实测；发行条件单独核定 |

## 立即下一步：P1 媒体健康与候选资格

按原下一增量计划 Task 3 继续：读取视频时长、画幅、帧率和音视频编码；空文件、损坏、无视频轨和非法时长不得进入自动候选；按文件签名缓存结果，首次跳过/待检项可在后续补探测。处理 FFprobe 超时、无效 JSON、工具不可用与单片失败，并用可注入探测器和小型真实视频测试验证。之后再实施 Task 4 旧台账可选描述。

实施工作目录固定为 `D:\GitHub\By\AU&PR\AU-PR-v071-20260815`。每项遵循 RED → GREEN → 回归 → 规格审查 → 质量审查；只暂存任务文件，不使用整仓 `git add .`。本轮用户已授权 Git 备份；远端推送仅使用该开发分支、正常快进，不覆盖其他分支。

## P0 已完成任务记录（保留原实施说明）

### Task P0.1: 缺失数据库的查询不得创建文件

**Objective:** `list_assets()` 名副其实只读，数据库不存在时给清楚错误并保持目录快照不变。

**Files:** Modify `source/dub_align_studio/material_catalog.py`; Test `tests/test_dub_align_catalog.py`.

**Step 1 — failing test:** 用临时 `library` 与库外 `cache/catalog.sqlite3`，不调用 `refresh()`，执行 `Catalog(db, library).list_assets()`；断言它抛出明确的数据库缺失异常，且 `db.exists()` 仍为 `False`。当前普通 `sqlite3.connect` 会创建 DB，测试应为 RED。

**Step 2 — verify RED:**

```powershell
$env:PYTHONPATH='source;tests'
$env:PYTHONIOENCODING='utf-8'
& 'D:\Python\Python311\python.exe' -m unittest test_dub_align_catalog.CatalogTests.test_list_assets_does_not_create_missing_database -v
```

**Step 3 — minimal implementation:** 查询前校验实际路径仍在库外，并用 SQLite URI 的 `mode=ro` 打开；Windows 文件 URI 必须由 `Path.as_uri()` 构造，再追加查询参数，而不是手拼 `file:C:\...`。若缺失，转换为稳定、可理解的错误。不要让这个修复重写 `refresh()`。

**Step 4 — verify GREEN:** 同一命令 PASS；再跑 `& 'D:\Python\Python311\python.exe' -m unittest test_dub_align_catalog -v`，预期全部目录测试通过。

**Step 5 — commit:** 仅暂存上述两个文件，提交 `fix(catalog): open catalog reads without creating files`。

### Task P0.2: 缓存目录重定向时拒绝查询

**Objective:** 构造对象后缓存父目录变成库内 junction/symlink，`list_assets()` 也不得在素材库内写入文件。

**Files:** Modify `source/dub_align_studio/material_catalog.py`; Test `tests/test_dub_align_catalog.py`.

**Step 1 — failing test:** 建立库外 `cache` 与库内目标目录，构造 `Catalog`，再把 `cache` 重定向至库内；调用 `list_assets()`，断言拒绝且库内目标目录/文件快照不变。若 Windows 环境无创建链接权限，另做受控 `Path.resolve` 测试，但须把“真实 junction 未验证”写进结果。

**Step 2 — verify RED:** `& 'D:\Python\Python311\python.exe' -m unittest test_dub_align_catalog.CatalogTests.test_list_assets_rejects_redirected_cache -v`；预期当前实现先造成库内数据库文件副作用。

**Step 3 — minimal implementation:** 在只读连接之前重新解析实际 DB 路径并拒绝位于素材库内的目标；保留 `mode=ro`，避免检查与打开间仍由默认连接自动建文件。无需声称解决恶意进程纳秒级重定向竞态。

**Step 4 — verify GREEN:** 定向测试和 `test_dub_align_catalog` 全部通过，并核对素材库快照。

**Step 5 — commit:** 仅暂存上述两个文件，提交 `fix(catalog): reject redirected read paths`。

### Task P0.3: 安全关口与回归归因

**Objective:** 关闭阻断缺陷后才允许 P1 使用真实素材库。

**Files:** Test `tests/test_dub_align_catalog.py`; read-only check `source/dub_align_studio/material_catalog.py` 与本轮 diff；必要时仅以新复现测试修复回归。

1. 运行 `test_dub_align_catalog.py`、`test_dub_align_storyboard.py`、`test_dub_align_source_trim.py`、`test_dub_align_web.py` 的定向回归；`git diff --check`。
2. 独立规格审查先确认库外 DB、源素材只读、root 绑定、稳定 ID 没有回归；独立质量审查再核对路径检查和 SQLite URI 真只读。任一未通过则留在 P0，不进入 P1。
3. 对既有 9 项广泛失败保留“已证实基点/疑似基点/本轮新增”分类；P4 发布门槛前，用隔离基点复跑 6 项未确认失败，避免误归因。

## P1–P7 实施文件和测试路由

- P1：扩展 `source/dub_align_studio/material_catalog.py`、`tests/test_dub_align_catalog.py`；旧台账若需任意列读取，最小扩展 `source/dub_align_studio/xlsx_reader.py` 和其测试。首次全库 FFprobe 耗时实测，不推测 ETA。详见原下一增量计划 Task 2–4。
- P2：新建 `source/dub_align_studio/material_retrieval.py`、`tests/test_dub_align_retrieval.py`，通过 `source/dub_align_studio/storyboard.py` 接入。详见原下一增量计划 Task 5–6。
- P3：修改 `source/dub_align_studio/storyboard.py`、`studio_pipeline.py`、`web_server.py`、`web/index.html`；扩展 `tests/test_dub_align_storyboard.py`、`tests/test_dub_align_web.py`，必要时新增 `tests/test_dub_align_edit_plan.py`。详见原下一增量计划 Task 7–8。
- P4：新建 `source/dub_align_studio/film_qa.py`、`tests/test_dub_align_film_qa.py`，接入 `studio_pipeline.py`/`web_server.py`；复测 `tests/test_dub_align_b.py`、`tests/test_dub_align_source_trim.py`。详见原下一增量计划 Task 9–10。
- P5：在任务输出目录生成 39 行可审查报告，不写原 XLSX 或 `D:\宇宙素材`。没有人工看片评分只能报告候选覆盖，不得宣称语义质量达标。
- P6：在官方最新订阅集成规则允许、用户本机完成授权后，新增独立桥接和假服务测试；不读取既有 Codex 私有凭据、不使用 API Key 冒充订阅、不上传完整视频或整库。实施时再确定具体 API/授权接口，不把旧蓝图中的文档快照当永久事实。
- P7：保留现有许可证/配音/FFmpeg 能力的边界；云端或收费发行需单独验证资格与成本，不因为个人本机原型成功就承诺可商业打包。

## 产品验收口径

最终用户动作仍是“选一份五列表格 → 选现有素材库 → 使用现有配音方案 → 直接生成成片”。剪辑方案、选片依据、缺片与人工复核清单、可播放 MP4 和技术质检是同一任务的输出。自动完成与画面贴切分开验收：三个试片技术通过且 39 镜头人工评审有记录，才讨论扩大到 145 份表格和 Codex 深度参与。

本文件创建时仅列规划；2026-10-08 更新执行状态并纳入 Git 备份。阶段完成以代码、测试与验收产物为准。
