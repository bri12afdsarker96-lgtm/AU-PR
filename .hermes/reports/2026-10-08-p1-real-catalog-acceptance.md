# P1 真实素材库验收记录（2026-10-08）

## 范围与基点

- 被验代码：`e6d1b0830ca283bd2e4857961fe07365fd421c3e`，分支 `codex/v071-20260815`。
- 本轮不改生产源码；验证已实现的可恢复媒体探测和可选旧台账导入。
- 源素材和原台账只读。缓存、完整路径清单、源表描述、快照及逐项探测日志仅保存在本机库外验收目录，不提交 GitHub。
- 本地证据目录：`D:\GitHub\By\AU&PR\catalog-validation\20261008-e6d1b08`。
- 健康仅指 FFprobe 元数据检查，单片超时 30 秒；不是完整解码检查，更不是画面语义匹配验收。

## 真实扫描（专项验收通过）

前置快照为 15,735 个条目：15,698 个常规文件、37 个目录；读取错误及 reparse/link 均为 0。支持格式的视频为 15,679 个，按现有规则排除 355 个（待人工复核 2、非宇宙内容 353），实际 eligible 为 15,324 个。排除项不计入检查通过。

| 阶段 | 进程 PID | 实际探测次数 | 实际刷新耗时 | 结论 |
| --- | ---: | ---: | ---: | --- |
| 首轮主动取消 | 24596 | 10 | 3.1 秒 | 10 条 playable 后 cancelled，15,314 条 pending |
| 新进程续跑 | 19684 | 15,314 | 1,527.1 秒 | 同一数据库，最初 10 条直接复用，全部分类完成 |
| 新进程重扫 | 30880 | 0 | 15.3 秒 | 15,324 条全部复用，签名未变化 |

最终为 **15,324 playable，0 unreadable，0 missing，0 pending**。`completed` 表示全部分类，不等于必然全可用；本次实测恰好全部为 playable。首次全库实际刷新合计 1,530.2 秒（约 25 分 30 秒），不含脚本准备、预检等待和缓存复扫。

源目录前后路径、大小、mtime_ns 逐项差异为 0；后置快照读取错误及链接同样为 0。源台账前后 SHA256、大小、mtime 一致。视频没有逐字节哈希，因此不声称排除了保持大小/时间戳的内容变化，或两次快照之间发生又恢复的瞬时变化。

## 旧台账与本地证据

- 9,071 个有效且唯一的 BJ 编号中，8,759 个精确关联当前入库资产，312 个没有匹配；不按口播或行号猜绑。这不代表 312 个对应文件一定丢失，也可能在排除范围或不在当前目录内。
- 8,759 条素材得到 E/G/H 共 26,277 条描述；两次导入数量及内容摘要完全一致，导入前后资产健康和签名记录不变。
- 库内 15,281 条带有效 BJ 前缀的素材中，6,522 条未匹配该台账；另有 43 条不带该格式前缀。缺少台账描述不会把健康素材改成坏片，但后续选片不能伪称有台账证据。
- `summary.json`、`README.md`、`健康清单.csv`、`异常清单.csv`、`排除清单.csv`、逐次探测日志、前后快照及 `catalog.sqlite3` 均保存在上述本地证据目录。
- CSV 不包含台账描述正文，字符串带公式注入防护；SQLite 含对应描述原文，仅本机保存。异常清单为空表（保留表头），不是漏生成报告。
- 独立规格复核 PASS、质量与集成终审 APPROVED：原始调用日志、完整候选集合、CSV、台账编号/行列来源与数据库交叉核对通过；源只读、库外输出、隐私边界和报告口径未发现剩余必修问题。数据库 `quick_check=ok`。

## 当轮回归（已执行）

- 定向 118 项通过：catalog 57、catalog_resume 26、storyboard 10、source_trim 3、legacy_metadata 19、旧 XlsxReaderTests 3。
- 更广的 `test_dub_align_*.py`：458 项，447 通过、9 失败、2 跳过，143.761 秒。
- 未把更广测试标记全绿，也未在本轮顺带修改渲染、工具解析或 Web 逻辑。失败仍须在成片/发行关口前归因收口。

当前失败列表（保留精确名称供下次定位，不凭相同数量认定全部已完成基点归因）：

1. `test_dub_align_b.EndToEndBRenderTests.test_verify_renders_frame_locked_film_with_audio`：成片 556 帧，预期 555，验收返回 1。
2. `test_dub_align_b.FfmpegPreflightTests.test_preflight_names_missing_binaries`：预期的缺工具异常未出现。
3. `test_dub_align_b.FfmpegPreflightTests.test_render_b_fails_friendly_before_touching_files`：先出现缺少音频错误，未出现预期 ffmpeg 提示。
4. `test_dub_align_b.RenderAuditRegressionTests.test_progressbar_framelock_on_exact_second_master`：181 帧，预期 180。
5. `test_dub_align_overlays.OverlayRenderTests.test_render_with_overlays_keeps_frame_lock`：346 帧，预期 345。
6. `test_dub_align_release_premiere.PremiereXmlTests.test_xml_pathurls_reference_final_material_not_staging`：Windows 路径解析后的存在性断言失败。
7. `test_dub_align_subtitles.BurnSubtitleRenderTests.test_render_with_subtitles_keeps_frame_lock`：346 帧，预期 345。
8. `test_dub_align_web.WebServerTests.test_generation_queue_runs_and_enters_edit_queue`：渲染收口断言失败，任务状态 failed。
9. `test_dub_align_web.WebServerTests.test_run_all_via_api`：渲染收口断言失败，job.ok 为 false。

## 后续边界

本次通过也只表示 P1 后台和真实素材目录验收通过。正式检索、主界面进度、三部真实成片、39 镜头语义评审及 Codex 订阅实调仍未完成。下一实现任务为 P2：B 口播、D 画面、E 运动分项检索与证据，随后加入时长/画幅约束和明确缺片状态。
