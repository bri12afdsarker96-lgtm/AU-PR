# PR #7 R14-FIX-3 推送与声明复核报告

复核日期：2026-08-14。复核对象：PR #7（`claude/excel-batch-commerce-video-ldkpit`）R14-FIX-3 轮次推送（`7a1f300..014a64c`）及其在 PR 正文中的全部声明。复核环境：Python 3.11.15 / ffmpeg 6.1.1-3ubuntu5 / pytest（独立容器，与开发轮容器不同）。

## 结论

**上一轮的推送操作与关键声明全部核实属实，无虚报。** 另有一项新发现：bulk_dub 测试套件在资源紧张环境下存在时序偶发失败（flaky），不影响本轮结论，建议后续轮次关注。

## 逐项核实

### 1. 推送与 PR 状态（GitHub API + git 双向核实）

| 声明 | 核实结果 |
| --- | --- |
| PR #7 open / Draft / 未合并 | ✅ `state=open, draft=true, merged=false` |
| head = `014a64c`（分支 `claude/excel-batch-commerce-video-ldkpit`） | ✅ GitHub API 与 `git ls-remote` 一致 |
| base = `claude/mercury-dubbing-volume-control-2r13zw` @ `8b2a2c1` | ✅ |
| fast-forward 推送（`7a1f300..014a64c`，未 force / 未 rebase） | ✅ `git merge-base --is-ancestor 7a1f300 014a64c` 成立；提交链 `ba91e01 → … → 7a1f300 → 014a64c` 完整 |
| 未新建 PR / 未改 base | ✅ |

### 2. 提交内容（`014a64c`）

- ✅ 6 文件，**+1069 / -383**，与声明逐字吻合（api.py +18/-…、ffmpeg_pipeline.py 13、gpu_profile.py 231、scheduler.py 94、service.py 385、test_bulk_dub_r14_fix2.py 711）。
- ✅ 关键新符号全部在位：`BenchmarkProcessRegistry` / `_popen_bench` / `validate_ladder` / `MAX_LADDER_STEPS=16` / `_validate_and_repair_loaded_profile` / `_terminate_and_wait`（gpu_profile.py）；`active_counts` / `_coordinator_needs_restart` / `_join_and_restart_coordinator_if_needed`（scheduler.py）；`_benchmark_generation` / `_finalize_error` / 状态机 `idle→preparing→…`（service.py）。
- ✅ 逻辑抽查：
  - **P0-1**：`service.stop()` 已无 `from integrated_workbench.proc import _ACTIVE`，只 `registry.terminate_all()` 收本次 benchmark 自己的进程（service.py:147）。
  - **P0-6**：`render_single` VideoError 硬件路径为单一所有权——有 `hw_failure_cb` 时不再直接调 `breaker.record_failure()`，仅无 callback 时兜底（ffmpeg_pipeline.py:431-439）。
  - **P0-7**：API 层调 `validate_ladder`，非法 ladder → HTTP 400（api.py:315-321）。

### 3. 测试复跑（独立容器）

| 声明 | 复核结果 |
| --- | --- |
| R14-FIX-3 专项 24 passed / 0 failed | ✅ 24 passed（6.95s） |
| bulk_dub 全量 260 passed / 0 failed | ⚠️ **可复现但不稳定**：三次运行分别为 1 failed/259 passed、3 failed/257 passed、**260 passed/0 failed**。失败项每次不同（`test_fix2_02` / `test_fix2_05` / `test_r12_1_leader_success_propagates_to_followers` 等），单文件独跑均通过——属时序偶发（flaky），非确定性回归 |
| 全库 731 passed / 8 failed | ✅ 完全复现（142s），失败即声明的 4 个 dub_align 文件共 8 项 |
| 8 项失败为 base 预存量 | ✅ 在 base `8b2a2c1` 上复跑同一批文件：**同一 8 项失败**；本轮未新增失败 |
| 关键断言抽查 | ✅ `test_fix3_g` 确断言 `consec_failures == 1`；`test_fix3_a`（并发 CAS）、`test_fix3_d`（stop 隔离）、`test_fix3_h`（ladder 400）均在位 |

### 4. 新发现（不阻塞，供下轮参考）

bulk_dub 套件全量运行时存在**时序敏感的偶发失败**，在复核容器（CPU 争抢较重）下约 2/3 概率出现 1~3 项失败，失败项漂移：

- `test_fix2_02_benchmark_only_queues_no_running`
- `test_fix2_05_stop_start_no_double_coordinator`
- `test_r12_1_leader_success_propagates_to_followers`

三者单独复跑均稳定通过，怀疑是测试中的等待窗口/超时阈值对慢机不够鲁棒，而非产品代码竞态；但 fix2_02/fix2_05 恰好覆盖 P0-2/P0-5 的并发路径，建议下一轮加固这几项的等待逻辑（轮询代替固定 sleep、放宽 deadline），以免掩盖真实回归。

### 5. 诚实边界（维持原声明）

无独立 GPU 环境，NVENC/QSV/AMF 真机路径、10000/日产能、24h 耐久本轮复核同样**未验证**——与 PR 正文的"未验证 / 保留"清单一致，无越界宣称。

---

# 追加：R14-FIX-4 复核（2026-08-14 第二轮）

复核对象：`014a64c..e6cb2c2`（测试鲁棒性整改）。

## 结论

**R14-FIX-4 通过复核，验收达标，可进入人工操作机打包测试。** 修复方向正确且未削弱断言强度；另发现一个残留小缺口（非阻塞）。

## 逐项核实

| 声明 | 核实结果 |
| --- | --- |
| fast-forward `014a64c..e6cb2c2`，未 force/rebase | ✅ `merge-base --is-ancestor` 成立 |
| PR #7 head = `e6cb2c2`，Draft/open/未合并，base 未动 | ✅ GitHub API 确认 |
| 只动 2 个测试文件（+98/-31），`source/` 0 改动 | ✅ `git diff --stat 014a64c e6cb2c2 -- source/` 为空 |
| 未删/未跳过测试来隐藏问题 | ✅ 260 项全数存在；diff 审查确认断言为**加强**而非削弱 |

## 断言强度审查（逐处 diff）

- `test_fix2_02`：sleep(1) 单点断言 → `_stays_true(status==PENDING, 3s)` 持续探测——**加强**
- `test_fix2_05`：去掉全局 `threading.enumerate()`（误报根因：被并行/前序测试收敛中的 coord 污染），改为本实例核对，且**新增** `_wait_until(not coord1.is_alive(), 10s)` 死亡确认——合理缩小范围 + 补强
- `test_fix3_a`：sleep(0.05) 押注调度 → poll 状态至 preparing/running——**加强**
- `test_fix3_d`：sleep(0.2) 单点存活检查 → 持续 2 秒存活验证（负向断言）——**加强**
- `test_fix3_e`：sleep(0.2) 单点 → poll 至真死——合理
- `test_bulk_dub_r12.py`：`_mk_service_paused` 暂停 worker 消除观察窗口污染；传播断言仍走 store 层，验证目标未变
- `_stays_true` 探测抛异常即返回 False（fail-safe 方向正确）

## 独立验收复跑（本复核容器 = 此前 2/3 概率复现失败的环境）

- 正常负载连续 5 轮：round 2~5 全部 **260 passed / 0 failed**；round 1 为 259 passed / **1 skipped**（见下）
- 高负载（双份套件并行）连续 2 轮 × 2 副本 = 4 份：全部 **260 passed / 0 failed / 0 skipped**
- 对照：R14-FIX-3 `014a64c` 在本容器此前 3 次全量出现 1~3 项漂移失败——修复效果确凿

## 残留小缺口（非阻塞，建议下轮顺手修）

`_has_ffmpeg()`（tests/test_bulk_dub_r14.py:448 及同构副本）用 `subprocess.run(["ffmpeg","-version"], timeout=5)` 探测，慢机/冷启动下探测超时 → 相关测试被**静默 skip**（本轮 9 份运行中观测到 1 次）。skip 不计 fail，"0 failed" 验收表面达标但覆盖被悄悄缩水，与"不掩盖真实回归"的整改精神相悖。建议：模块级探测一次并缓存（如 `functools.lru_cache`），timeout 放宽到 15s；skip 时输出明确 reason 便于发现。
