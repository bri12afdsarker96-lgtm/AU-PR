# 水星剪辑 v2026.07.04.8

这是面向短视频剪辑、二创、标题封面和发布交接的一体化桌面客户端。

> **2026-07-09 结构精简迭代（当前状态）**：导航精简为四入口——项目与素材总览 / 配音对齐成片 / 矩阵生产 / 工具箱自检。
> 已删除功能：时间线剪辑（含内嵌 VLC 预览、快捷键、Premiere 工程导出）、文本剪辑时间线、字幕草稿、按描述找片段。
> 自动剪辑并入矩阵生产作为前置步骤；配音对齐成片独立入口并新增画面尺寸 1:1/4:3/3:4/9:16/16:9、剪映草稿导出；各类素材导入下放到对应模块。
> 下方历史条目为过往版本记录，保留不改写。

## 开发主源

后续开发以当前版本文档目录为主要开发源：

```text
D:\项目开发\水星剪辑\当前开发版本\docs
```

统一口径文件：

```text
D:\项目开发\水星剪辑\当前开发版本\docs\开发主源与统一口径_20260703.md
```

源码、构建、发布清单、安装副本和历史报告之间如有冲突，优先按上述文档和 `项目成果梳理与后续研发路线_20260703.md` 判断。

## 最短业务流程

1. 在“素材入库”创建或选择项目。
2. 点击各类素材的“选文件夹”，把原始视频、背景、贴图、音频、分镜表导入对应业务目录；少量补充素材可用“选文件”。
3. 到“剪辑与整合”填写目标时长并点击“一键自动剪辑”；需要细调时再展开“高级参数”。
4. 到“矩阵生产”填写账号数、素材来源和去重强度，点击“一键生产”。
5. 到“发布与回流”打开最新产线目录，按生产清单发布，再导入发布回写查看复盘。
6. 需要精修时切到“精修模式”，系统会保留更多中间产物，便于外部软件继续处理。
7. 工具箱按“可启用组件/开发者参考库”分级；开发者参考库不是即装即用能力，日常运营可忽略。

## 品牌与界面

- 软件名称统一为“水星剪辑”。
- Logo 使用水星行星、双层轨道、斜向剪辑切线和两个剪切节点，不沿用旧图标元素。
- 主程序、安装引导、窗口标题栏和侧边栏均使用 `assets/brand/shuixing_icon.ico/png`。
- 品牌资产可通过版本目录或发布包里的 `build_scripts/generate_brand_assets.py` 重新生成。

## 新目录表

- `00_项目配置`：project.json、publish_accounts.csv、项目目录表、分镜表模板。
- `01_素材入库/A_原始视频`：短剧原片、主素材。
- `01_素材入库/B_去重背景`：叠加背景、混剪底片。
- `01_素材入库/C_贴图素材`：贴纸、边框、角标、遮挡图。
- `01_素材入库/D_音频素材`：配音、BGM、音效。
- `01_素材入库/E_文案分镜`：分镜表、台词、字幕、文案。
- `02_自动剪辑/01_粗剪输入`：专门给自动剪辑筛选的候选片段。
- `02_自动剪辑/02_场景检测`：PySceneDetect 等插件输出的镜头切点结果。
- `02_自动剪辑/03_分镜素材包`：自动剪出的分镜片段、音频占位、导入清单。
- `02_自动剪辑/04_预览样片`：自动剪辑预览视频。
- `02_自动剪辑/05_字幕转写`：字幕草稿、台词时间轴、转写报告。
- `02_自动剪辑/06_文本剪辑`：台词剪辑表、文本剪辑计划、文本剪辑报告。
- `03_二创生产/01_中间结果`：二创输出和外部剪辑器回写结果。
- `03_二创生产/02_退回回收`：不可用视频和退回原因。
- `03_二创生产/03_合格待发布`：产线输出后可进入发布队列的视频。
- `03_二创生产/04_发布完成`：发布后归档。
- `03_二创生产/05_社媒切条增强`：竖屏裁切、响度标准化、BGM ducking 后的社媒增强版。
- `03_二创生产/06_成品批次档案`：按二创批次生成可追溯快照。
- `03_二创生产/07_整集重组`：长剧集切片、逐段去重变体和按原顺序合并后的整集版本。
- `04_标题封面/01_标题表`：titles.csv。
- `04_标题封面/02_封面图`：自动抽帧或人工放入的封面图。
- `04_标题封面/03_发布文案`：简介、话题、平台文案。
- `04_标题封面/04_包装素材`：海报封面、片头卡、片尾卡和包装清单。
- `04_标题封面/05_包装成片`：加片头片尾后的包装版视频。
- `05_发布交接/01_发布队列`：publish_queue.csv。
- `05_发布交接/02_剪辑交接包`：editor_handoff.json、任务 CSV。
- `05_发布交接/03_发布工具交接`：publish_handoff.json、导入 CSV。
- `05_发布交接/04_发布回写`：发布工具回传的 publish_status.csv/json 和导入结果。
- `00_项目配置/01_素材清单`：素材清单 CSV/JSON、入库记录。
- `00_项目配置/02_运行日志`：按日期记录操作日志。
- `00_项目配置/03_运行记录`：发布记录、流程快照和组件校验记录。

旧版 `01_原始视频_A`、`02_去重背景_B` 等目录仍兼容读取，但新项目默认使用上面的业务目录。

## 命令行能力

```powershell
python -m integrated_workbench.cli init --root D:\短剧项目 --name 项目名
python -m integrated_workbench.cli import-assets --project D:\短剧项目\项目名 --role 原始视频 D:\素材\a.mp4
python -m integrated_workbench.cli inventory --project D:\短剧项目\项目名
python -m integrated_workbench.cli edit --project D:\短剧项目\项目名 --target-seconds 60 --clip-seconds 4
python -m integrated_workbench.cli dub-assemble --project D:\短剧项目\项目名 --audio-dir D:\配音包 --video-dir D:\分镜视频
python -m integrated_workbench.cli transcribe --project D:\短剧项目\项目名 D:\素材\a.mp4 --text "第一句台词。第二句台词。"
python -m integrated_workbench.cli text-timeline --project D:\短剧项目\项目名 --keywords "反转,证据,冲突" --run
python -m integrated_workbench.cli description-search --project D:\短剧项目\项目名 --query "女主发现证据 冲突爆发" --run
python -m integrated_workbench.cli recommend-edit --project D:\短剧项目\项目名 --goal "60秒冲突反转高光" --run
python -m integrated_workbench.cli edit --project D:\短剧项目\项目名 --strategy 爆点融合 --keywords "反转,证据,冲突"
python -m integrated_workbench.cli edit --project D:\短剧项目\项目名 --strategy 字幕关键词 --keywords "反转,秘密,证据"
python -m integrated_workbench.cli edit --project D:\短剧项目\项目名 --strategy 音量高光 --target-seconds 30
python -m integrated_workbench.cli render --project D:\短剧项目\项目名 --copies 3
python -m integrated_workbench.cli episode-rework --project D:\短剧项目\项目名 --input D:\素材\长剧集.mp4 --versions 3 --dedup-level balanced
python -m integrated_workbench.cli titles --project D:\短剧项目\项目名
python -m integrated_workbench.cli visual-package --project D:\短剧项目\项目名 --videos
python -m integrated_workbench.cli visual-package --project D:\短剧项目\项目名 --template 解说口播 --videos
python -m integrated_workbench.cli init-accounts --project D:\短剧项目\项目名
python -m integrated_workbench.cli social-clips --project D:\短剧项目\项目名 --source ready
python -m integrated_workbench.cli queue --project D:\短剧项目\项目名
python -m integrated_workbench.cli queue --project D:\短剧项目\项目名 --source social --auto-account
python -m integrated_workbench.cli queue --project D:\短剧项目\项目名 --source packaged --auto-account
python -m integrated_workbench.cli capability-check
python -m integrated_workbench.cli install-whisper --model tiny
python -m integrated_workbench.cli verify-workflow
python -m integrated_workbench.cli verify-release --smoke
python -m integrated_workbench.cli github-radar
python -m integrated_workbench.cli export-editor --project D:\短剧项目\项目名
python -m integrated_workbench.cli export-publish --project D:\短剧项目\项目名
python -m integrated_workbench.cli import-editor-result --project D:\短剧项目\项目名 --file D:\短剧项目\项目名\05_发布交接\02_剪辑交接包\editor_result.csv
python -m integrated_workbench.cli import-publish-status --project D:\短剧项目\项目名 --file D:\短剧项目\项目名\05_发布交接\04_发布回写\publish_status.csv
python -m integrated_workbench.cli mark-failed --project D:\短剧项目\项目名 --reason "平台发布失败" --limit 1
```

下载已授权素材：

```powershell
python -m integrated_workbench.cli download --project D:\短剧项目\项目名 --url "https://example.com/video" --target 原始视频
```

## 已纳入能力组件

（历史记录，当前版本见主源口径文档）

- 授权素材下载：下载已授权视频素材并自动入库。
- 去静音粗剪：按静音、动作、黑场等规则做基础粗切。
- 字幕找片段：用字幕、台词和关键词定位剧情片段。
- 高光片段提取：从长素材里筛选冲突、反转、情绪高点。
- 剪映草稿交接：导出草稿包，方便继续人工精修。
- 动态包装渲染 / 程序化视频模板：服务片头、片尾、字幕动画和动态封面。
- 智能动作链 / 视频工厂架构：把复杂目标拆成可审计任务步骤。
- 意图剪辑计划：把一句剪辑目标转成策略和分镜计划。
- 文本剪视频：把字幕草稿升级为“选文字/删文字就是剪辑”。
- 按描述找片段：按人物、动作、场景描述筛选素材。
- 社媒切条增强：v2026.07.01.13 已先落地竖屏裁切、响度标准化、BGM 自动循环和说话时压低音乐。
- 能力组件自检：检查 FFmpeg、授权素材下载组件、运行依赖和每项能力组件状态，输出 Markdown/JSON/CSV。
- 动效包装技能 / 程序化包装组件：后续用于片头片尾、字幕动画和封面动效模板。
- 目标式剪辑流程 / 自动剪辑技能手册：用于沉淀素材分析、剪辑计划和复核步骤。
- 常用转码预设 / 底层引擎更新源：用于转码、压缩、音量统一和版本检查。
- 本地语音转写、字幕格式修复、多语言字幕配音、无损复核切割等能力作为可选增强。

完整筛选结论见 `docs/github_tool_radar.md`、`docs/github_research_matrix.md` 和 `docs/github_live_repository_analysis.md`。部分工具是源码纳入或雷达候选状态，需要编译、安装依赖或确认许可证后才能直接运行。工作台优先保证核心流程可用，再逐步把这些工具封装为按钮。

## v2026.07.06 模块精简

- 移除旧状态评分驾驶舱、旧人工复核入口和旧视频检测报告入口。
- 新项目不再创建旧 `06` 报告目录，素材清单、运行日志和运行记录统一写入 `00_项目配置`。
- 批次档案不再写入旧检测报告字段，成片追溯改为记录“流转状态”。
- `verify-workflow` 删除旧检查项，后续新增能力在精简后的验收基线上累加。
- 新增 `episode_pipeline.py` 和命令 `episode-rework`，支持整集切片、逐段去重生成多版本，再按原顺序合并成 N 条完整整集。
- `verify-workflow` 已回升到 31 项验收，新增“整集切片重组”检查，同时覆盖多切点切片和零切点兜底。
- 工具箱参考库按“可下载运行产物 / 开发者参考源码 / 架构参考（开发者）”分治，分类依据见 `docs/工具箱能力分级_20260706.md`。

## v2026.07.04.8 安全收口与音频指纹补齐

- 收编两个未随 tag 发布的加固提交（移除授权 XOR 降级后门、pyarmor 探雷证据），v2026.07.04.7 安装包停止分发。
- 去重指纹报告新增音频指纹：主引擎 Chromaprint fpcalc（工具箱可下载，外部调用），未下载自动回退 FFmpeg 能量包络；新增 `音频相似度明细_*.csv`。
- 工具箱新增可下载组件：音频指纹检测（fpcalc）、画质增强超分（Real-ESRGAN ncnn-vulkan）；新增 ImageHash、SoundFingerprinting、FFMpegCore、Piper 参考条目，能力自检 39 项扩展为 45 项。
- 新增 `tests/` 单元测试与 GitHub Actions 流水线（Linux 编译+单测、Windows 双环境业务验收、手动触发的打包+发布验收）。
- 详见 `docs/版本发布_v2026.07.04.8_20260706.md` 与 `docs/工具雷达补充_NuGet与GitHub接入_20260706.md`。

## v2026.07.04.7 正式安装包收口

- 自 v2026.07.04.6 源码 tag 后正式构建安装包，纳入删除项目体检/审核质检、整集切片重组流水线、整集重组 UI、参考库分治和螃蟹到水星命名清理。
- 业务验收基线从 34 项调整为 31 项，原因是旧项目体检、旧人工复核入口和旧视频质检报告已删除；整集重组作为新验收项留在精简后的基线上。
- APP ID 固定不变，授权 User-Agent 随构建版本升为 `ShuiXingJianJi/v2026.07.04.7`。
- 轻量安装策略不变：模型、whisper-cli、PySceneDetect 和 scenedetect 仍不进入安装包。

## v2026.07.01.13 GitHub 逐项分析与社媒增强

（历史记录，当前版本见主源口径文档）

## 当前补齐：教学视频反查缺口落地

- 自动剪辑页新增“片段描述”：可输入人物、动作、场景或台词意图，生成描述匹配表，也可直接剪成分镜素材包和预览样片。
- 新增 `description_search.py` 和命令 `description-search`，按字幕/台词/文案分镜、文件名和画面候选生成可复核剪辑计划。
- 新增 `install-whisper` 命令和界面按钮“安装真实转写运行时”，可下载 whisper.cpp Windows 运行包和 tiny 模型，安装后“本地语音转写”在能力自检中显示可用。
- 发布交接页新增剪映草稿依赖状态，区分“脚本包/时间线 CSV”和“真实 CapCut 草稿”。
- `verify-workflow` 已扩展到 20 项验收，新增“描述找片段”等检查，避免教学视频功能与软件实际落地脱节。
- 标题封面页新增标题候选和智能封面候选评分表，标题不再只有单条结果，封面也会保留可复核的多帧候选。
- 包装模板新增“冲突开场、解说口播、沉浸预告”，片头片尾视频和最终包装成片都会套用对应动效。
- 发布交接页新增“导入剪辑器回写、导入发布回写、标记发布失败”，本地剪辑器和发布工具可以把结果写回工作台。
- C# 交接层新增发布状态回写和剪辑器回写 CSV 写入器，方便外部桌面壳、剪辑器和发布工具深度联动。
- `verify-workflow` 已扩展到 24 项验收，新增“剪辑器回写导入、标题候选与智能封面、动态包装模板、发布状态回写”检查。

- 抓取用户给出的 10 个 GitHub 搜索入口前排仓库，生成 `docs/github_search_live` 快照。
- 新增 `github_live_analysis.py`，输出 `github_live_repository_analysis.md/json/csv`，逐项记录采纳、排除、可融入板块和第一动作。
- 下载并登记 `opensource-clipping`、`motion-skills`、`remotion-com-skills`、`agentic-video-editor`、`ai-video-editing-skill`。
- 新增 `social_clipper.py` 和命令 `social-clips`，可从合格待发布或中间结果视频生成社媒增强版 MP4。
- 发布队列 `auto` 来源现在优先级为：包装成片 > 社媒增强版 > 合格待发布。
- “流程总览”改为完整业务目录表，不再只展示部分目录，减少普通用户放错文件的概率。
- 工具箱新增“能力自检”，输出组件状态报告。
- 新增 `verify-release` 发布包验收命令，检查主程序、安装包、zip、品牌资产、源码模块、命名纯净度和启动烟测。
- 新增 `verify-workflow` 业务流程验收命令，会自动生成样本素材并跑通项目创建、素材入库、自动剪辑、二创、标题封面、包装成片、社媒增强、发布队列、剪辑交接、发布交接和发布回流。

## v2026.07.04.5 功能收官构建

- 汇入机器代验轮的小债清偿：纯净审计自排除、CLI 中文输出可读、发布队列回写匹配修复。
- 汇入模型库可靠性轮：`MODEL_REGISTRY` 登记 whisper-cli 和 tiny/base/small 模型的官方 size/sha256，下载支持 `.part` 断点续传、重试、切源、校验失败删除失败下载物。
- 工具箱模型库新增“未下载 / 下载中 x% / 校验中 / 校验通过·可用 / 文件损坏”状态口径，并支持项目级 `00_项目配置/模型源.json` 覆盖模型源 URL。
- 字幕草稿接入真实 whisper 转写：可用时输出 `engine: real(whisper-<model>)`，不可用或失败时保留 `engine: mock_timeline` 与 `fallback_reason`。
- 修复中文安装路径下 whisper-cli 读取模型路径失败的问题：真实转写调用使用运行目录内 ASCII 相对临时路径。
- 本版为功能开发收官版；构建、验收、机器代验、推送与 tag 完成后进入开发冻结。

## v2026.07.04.4 发版收口

- 发布状态导入正式汇入“发布与回流”记录，队列状态和复盘口径统一落库。
- “剪辑与整合”页新增配音对齐成片直通生产：选配音包、选分镜视频、点“对齐并合成”，默认完成后直接进入矩阵一键生产。
- 配音包和分镜文件夹会按项目记住上次成功路径；旧项目缺字段时按空路径兼容。
- 生产驾驶舱主动作卡新增“配音对齐生产”，可直接跳到配音对齐卡。
- “发布与回流”页新增发布助手（引导发布），只打开平台入口、复制文案和定位本地视频，发布动作仍由人工完成。
- 运营手册、UI 冒烟清单和发布前人工验证清单统一升级到 v2026.07.04.4。

## v2026.07.04.3 易用性打磨

- 素材入库主交互改为“选文件夹”，支持同类素材批量导入，保留“选文件”作为补充入口。
- 业务验收扩展到 31 项，新增“素材入库/文件夹导入”自动检查。
- “矩阵生产”页只保留“账号数”，一键生产和生成中间结果共用该数量。
- “剪辑与整合”默认只露出目标时长和一键自动剪辑，高级参数默认折叠。
- 工具箱按“可启用组件”和“开发者参考库”分区展示，新增标准库实现的组件下载入口。

## v2026.07.01.12 文本剪辑时间线

- 新增 `text_timeline.py`，把 CutScript 的“文本剪视频”思路落成本地可执行能力。
- 新增目录 `02_自动剪辑/06_文本剪辑`，存放 `台词剪辑表.csv`、`文本剪辑计划.csv` 和 `文本剪辑报告.json`。
- 自动剪辑页新增“文本剪辑时间线”：可从最新字幕草稿生成台词剪辑表，再按“保留”列应用生成分镜素材包和预览样片。
- 命令行新增 `text-timeline`：可从字幕 CSV/SRT/JSON 生成台词表，也可应用用户编辑后的台词表。
- 文本剪辑表和文本剪辑计划纳入业务流程验收，避免文本剪辑产物脱离主流程。

## v2026.07.01.11 GitHub 剪辑能力补强

- 重新复核用户给出的 10 个 GitHub 搜索入口，新增“搜索入口逐项筛查”，每个入口都记录采纳或排除理由。
- `github_research.py` 从候选矩阵升级为“筛查记录 + 功能融入路线 + 候选矩阵”，生成的 Markdown/JSON/CSV 都包含 48 个候选和 57 条筛查记录。
- 自动剪辑页新增 5 条“按功能融入路线”：自动剪辑入口、二创包装成片、剪映精修交接、工具底座、Agent 任务编排。
- 自动剪辑增强路线扩展到 12 个重点仓库，新增 CutScript、PreenCut、opensource-clipping、Director、chengfeng-videocut-skills、timeline-studio 等方向。
- 工具箱新增 CutScript、PreenCut、opensource-clipping、Director、chengfeng-videocut-skills、timeline-studio、FFmpegFreeUI、BtbN FFmpeg Builds 的能力状态登记。
- 已下载并展开 CutScript、PreenCut、Director、chengfeng-videocut-skills、FFmpegFreeUI；`opensource-clipping` 和 `timeline-studio` 因 GitHub 下载包未完整展开，保持待下载状态。

## v2026.07.01.10 包装发布队列闭环

- 发布队列默认来源改为 `auto`：有包装成片时优先发布包装版视频，没有包装版时回退到合格待发布视频。
- 发布队列新增字段 `source_type` 和 `original_video_path`，能看清发布视频来自包装成片还是原待发布视频。
- 发布页新增“启用测试账号”，可一键把默认账号模板改成可用本地测试账号，避免普通用户必须手改 CSV 才能生成队列。
- 命令行新增 `init-accounts`，`queue` 新增 `--source auto|ready|social|packaged` 和 `--auto-account`。
- 发布交接包会记录 `packaged_video_dir`，发布工具导入队列也能携带包装来源字段。

## v2026.07.01.09 标题封面包装层

- 新增 `visual_package.py`，可从待发布视频、标题表和封面抽帧生成竖屏海报、片头卡、片尾卡。
- 标题封面页新增“生成海报/片头尾”和“生成包装成片”。
- 命令行新增 `visual-package`，支持只生成包装素材，也支持 `--videos` 生成加片头片尾的包装版视频。
- 包装素材写入 `04_标题封面/04_包装素材`，包装成片写入 `04_标题封面/05_包装成片`，并生成 CSV/JSON 清单。
- 包装素材和包装成片纳入业务流程验收，让标题封面不再只是抽帧和基础文案。
- 这一步对应 HyperFrames、Remotion、editly 的“模板包装”价值，但先采用本地 Pillow + FFmpeg 的轻量可运行实现。

## v2026.07.01.08 爆点融合自动剪辑

- 自动剪辑默认策略改为 `爆点融合`，普通用户不用先理解字幕、音量、镜头这些专业参数。
- `爆点融合` 会把字幕关键词、音量高光、去静音保留段和镜头切点放进同一张候选表，按剧情词、情绪词和多信号重叠进行评分。
- `智能粗剪` 也会先尝试融合评分，不再只是简单回退到单一策略。
- 对应 GitHub 分析里的 FunClip、AutoClip、Auto-Editor、PySceneDetect 和 video-use 思路，当前用本地 FFmpeg/字幕文件实现可运行版本，后续可替换为外部模型增强。
- 推荐策略入口现在更偏“一句话目标”：例如输入“60秒冲突反转高光”，会默认走爆点融合并生成推荐报告。

## v2026.07.01.07 字幕草稿与文本找片段

- 新增 `transcription.py`，生成字幕草稿、台词 CSV 和转写 JSON。
- 自动剪辑页新增“生成字幕草稿”和“打开字幕目录”。
- 命令行新增 `transcribe`：有 whisper.cpp 可执行文件和模型时优先转写；没有模型时用静音检测生成可校对的台词时间轴。
- 生成结果会保存到 `02_自动剪辑/05_字幕转写`，并同步到 `01_素材入库/E_文案分镜`，供“字幕关键词”策略直接读取。
- 可粘贴参考台词，系统会按句子分配到时间轴，适合已有解说稿或短剧台词初稿。

## v2026.07.01.06 旧检测模块

- 该历史模块已在 v2026.07.06 精简中移除，当前版本不再提供对应界面入口、命令行入口或报告目录。

## v2026.07.01.05 GitHub 自动剪辑矩阵

- 新增 `github_research.py`，把用户给出的 10 个 GitHub 检索入口统一整理成候选矩阵。
- 新增 `github-radar` 命令，会生成 Markdown、JSON、CSV 三份分析文件。
- 自动剪辑页面新增“自动剪辑增强路线”，把 Auto-Editor、FunClip、AutoClip、FireRed-OpenStoryline、video-use、jianying-editor-skill、pyCapCut 映射到具体业务入口。
- 自动剪辑页面新增“按目标推荐策略”，用户输入一句剪辑目标，软件会自动选择字幕关键词、音量高光、去静音粗剪、镜头节奏粗剪或顺序分镜，并保存推荐报告。
- 新增命令：`python -m integrated_workbench.cli recommend-edit --project D:\短剧项目\项目名 --goal "60秒冲突反转高光" --run`。
- 工具箱新增“生成来源报告”，刷新能力清单时同步输出候选仓库、许可证、风险和接入位置。
- 对 GPL/AGPL 或许可证不明确的仓库只做外部调用或流程参考，不直接把代码混入客户端。

## v2026.07.01.02 剪辑增强

- `字幕关键词`：借鉴 FunClip 的“字幕文本定位片段”思路，读取 `01_素材入库/E_文案分镜` 中的 SRT/CSV，按关键词生成剪辑计划。
- `音量高光`：借鉴 AutoClip/video-use 的“高能片段筛选”方向，使用 FFmpeg 解码音频并按 RMS 音量窗口提取高能段。
- `智能粗剪`：优先尝试字幕关键词和音量高光，再回退到去静音、镜头变化和顺序分镜。

## v2026.07.01.03 剪映草稿交接

- 发布队列新增“导出剪映草稿包”，会读取最新自动剪辑分镜包，复制分镜素材，生成 `capcut_timeline.csv`、`create_capcut_draft.py`、`capcut_draft_manifest.json` 和使用说明。
- 如果运行环境具备 pyCapCut，系统会尝试创建真实 CapCut/剪映草稿，草稿内包含视频轨和自动剪辑片段。
- 如果依赖不完整，软件仍会稳定生成可交接草稿包，用户可后续补齐依赖后运行包内脚本。
- 命令行入口：`python -m integrated_workbench.cli export-capcut --project D:\短剧项目\项目名 --create-draft --draft-root D:\CapCut Drafts`。

## v2026.07.01.04 旧状态评分驾驶舱

- 该历史模块已在 v2026.07.06 精简中移除，当前版本以生产驾驶舱、产线报告、发布回流复盘和能力自检报告承接日常状态判断。
