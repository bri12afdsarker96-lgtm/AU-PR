"""待编辑队列（⑧）与生成任务队列（⑨）测试。"""

import tempfile
import unittest
from pathlib import Path

from dub_align_studio import edit_queue, web_server


class UiContractPhase3Tests(unittest.TestCase):
    """界面契约：Phase 3 前端接线必须存在真实端点/元素（防「样子货」回归）。"""

    def test_index_wires_queues_and_preview(self):
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for m in ("enqueueTask", "/api/queue", "/api/edit_queue",       # ⑨⑧ 端点
                  'id="genQueueCard"', 'id="eqList"', "loadEditQueue",   # 两条队列 UI
                  'id="pvVideo"', "pvToggle", "stylePvBar",              # ⑦ 真视频预览
                  "烧录列表", 'id="subsize"', 'id="pbEnable"',           # ④⑥ 字幕/进度条移入一键成片
                  "applyPbObj", "applySubObj",                           # ⑤ 预设含字幕/进度条
                  'id="wmEnable"', "collectWatermark", "动态水印",        # 动态水印
                  "retryQueueTask", "action:'retry'",                    # 队列重试
                  "function toast", "tap-pulse", "pointerdown", ".busy",  # 全局点击反馈
                  "pickDoc", "mirrorOut", "_outdirManual",                # ① 导入设目录 + 输出跟随分镜
                  "dubLastVoice", "dubLastAspect",                        # ② 记住上次音色/比例
                  'id="burnPreset"', "saveBurnPreset", "collectBurnStyle",  # ③ 烧录预设
                  "enqueueTask(true)", "pauseResumeQueue", "cancelQueueTask",  # 生成成片并入队列 + 暂停/取消
                  'id="queuePauseBtn"', "无阶段进度", "qRunLabel", "准备素材"):  # 暂停按钮 + 心跳看门狗（温和提示）
            self.assertIn(m, html, m)

    def test_retry_reuses_existing_master(self):
        work = Path(tempfile.mkdtemp())
        try:
            out = work / "out"
            out.mkdir()
            (out / web_server.pipeline.MASTER_NAME).write_bytes(b"RIFF")
            payload = {"output_dir": str(out)}
            self.assertTrue(web_server._prefer_reuse_dub_if_master_exists(payload))
            self.assertTrue(payload["reuse_dub"])

            missing = {"output_dir": str(work / "missing")}
            self.assertFalse(web_server._prefer_reuse_dub_if_master_exists(missing))
            self.assertNotIn("reuse_dub", missing)
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)

    def test_queue_pause_and_cancel(self):
        import tempfile
        from pathlib import Path
        from dub_align_studio import web_server as ws
        ws._gen_queue_set_paused(True)                      # 暂停 → 不启动新任务，入队停 pending
        try:
            work = Path(tempfile.mkdtemp())
            tid = ws._gen_enqueue({"text": "一\n二", "engine": "mock", "aligner": "均分兜底",
                                   "output_dir": str(work / "o"), "shots_dir": str(work)}, "T")
            snap = ws._gen_queue_snapshot()
            self.assertTrue(snap["paused"])
            row = next(t for t in snap["tasks"] if t["id"] == tid)
            self.assertEqual(row["status"], "pending")
            self.assertTrue(ws._gen_queue_cancel(tid))       # 取消待办 → 移除
            self.assertFalse(any(t["id"] == tid for t in ws._gen_queue_snapshot()["tasks"]))
        finally:
            ws._gen_queue_set_paused(False)

    def test_pipeline_overlaps_dub_and_render(self):
        """流水线：任务 A 渲染时任务 B 应能并行配音（配音串行、渲染串行，但两阶段跨任务重叠）。"""
        import tempfile
        import threading
        import time
        from pathlib import Path

        from dub_align_studio import web_server as ws

        ws._gen_queue_set_paused(False)
        events: list[tuple] = []
        elock = threading.Lock()

        def rec(label: str, phase: str, kind: str) -> None:
            with elock:
                events.append((time.time(), label, phase, kind))

        orig = {n: getattr(ws.pipeline, n) for n in ("select_shot_videos", "step_dub", "step_timing", "step_render")}
        orig_remember = ws._remember_edit_item

        def fake_select(*a, **k):
            return []

        # 并发计数器：证明「配音一次只 1 个、渲染一次只 1 个」——若锁失效同时跑 2 个，峰值会 >1
        cnt = {"dub": 0, "render": 0, "dub_max": 0, "render_max": 0}
        clock = threading.Lock()

        def _enter(kind):
            with clock:
                cnt[kind] += 1
                cnt[kind + "_max"] = max(cnt[kind + "_max"], cnt[kind])

        def _exit(kind):
            with clock:
                cnt[kind] -= 1

        def fake_dub(text, engine_key, output_dir, voice, options, log=None, progress=None, heartbeat=None):
            label = Path(output_dir).name
            _enter("dub"); rec(label, "dub", "start"); time.sleep(0.2); rec(label, "dub", "end"); _exit("dub")
            (Path(output_dir) / ws.pipeline.MASTER_NAME).write_bytes(b"")
            return type("M", (), {"seconds": 1.0, "engine": engine_key, "path": Path(output_dir) / ws.pipeline.MASTER_NAME})()

        def fake_timing(text, master_path, aligner_key, output_dir):
            return [], []

        def fake_render(master_path, timings, videos, output_dir, style, **k):
            label = Path(output_dir).name
            _enter("render"); rec(label, "render", "start"); time.sleep(0.2); rec(label, "render", "end"); _exit("render")
            return type("R", (), {"ok": True, "output_path": Path(output_dir) / "成片.mp4", "subtitle_note": ""})()

        ws.pipeline.select_shot_videos = fake_select
        ws.pipeline.step_dub = fake_dub
        ws.pipeline.step_timing = fake_timing
        ws.pipeline.step_render = fake_render
        ws._remember_edit_item = lambda *a, **k: None
        try:
            root = Path(tempfile.mkdtemp())
            ids, names = [], ["A", "B", "C", "D", "E"]   # 压力：5 个任务同时压队列
            for name in names:
                out = root / name
                out.mkdir()
                ids.append(ws._gen_enqueue(
                    {"text": "一\n二", "engine": "mock", "aligner": "均分兜底",
                     "output_dir": str(out), "shots_dir": str(root), "burn_subtitles": False},
                    name))
            deadline = time.time() + 40
            while time.time() < deadline:
                snap = {t["id"]: t["status"] for t in ws._gen_queue_snapshot()["tasks"]}
                if all(snap.get(i) in ("done", "failed", "cancelled") for i in ids):
                    break
                time.sleep(0.1)
            final = {t["id"]: t["status"] for t in ws._gen_queue_snapshot()["tasks"]}
            # ① 全部成功、无任务失败（并发未互相打断）
            self.assertTrue(all(final.get(i) == "done" for i in ids), f"有任务未成功：{final}")
            # ② 配音永远只 1 个、渲染永远只 1 个（锁生效、GPU/ffmpeg 不被抢）
            self.assertEqual(cnt["dub_max"], 1, "同时有 >1 个配音在跑（配音锁失效）")
            self.assertEqual(cnt["render_max"], 1, "同时有 >1 个渲染在跑（渲染锁失效）")

            def interval(label, phase):
                st = next(t for t, l, p, k in events if l == label and p == phase and k == "start")
                en = next(t for t, l, p, k in events if l == label and p == phase and k == "end")
                return st, en
            # ③ 确有跨任务重叠：存在「某任务渲染」与「另一任务配音」时间相交（否则退化为串行）
            dubs = [(l, *interval(l, "dub")) for l in names]
            rends = [(l, *interval(l, "render")) for l in names]
            overlap = any(rl != dl and rs < de and ds < re
                          for dl, ds, de in dubs for rl, rs, re in rends)
            self.assertTrue(overlap, "未发生任何跨任务重叠（退化为串行）")
        finally:
            for n, f in orig.items():
                setattr(ws.pipeline, n, f)
            ws._remember_edit_item = orig_remember

    def test_browse_lists_files_and_parse_by_path(self):
        import tempfile
        from pathlib import Path
        from dub_align_studio import web_server
        d = Path(tempfile.mkdtemp()); (d / "sub").mkdir()
        (d / "稿.txt").write_text("第一句\n第二句\n", encoding="utf-8")
        r = web_server._browse(str(d), "txt")
        self.assertIn("sub", r["dirs"])
        self.assertIn("稿.txt", r["files"])
        self.assertEqual(web_server._browse(str(d), "")["files"], [])  # 不给 files_ext 时不列文件


def _touch_file(p: Path) -> str:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00")
    return str(p)


class EditQueueTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eq_"))
        self.store = self.root / "edit_queue.json"

    def _film(self, name: str) -> str:
        return _touch_file(self.root / name / "成片.mp4")

    def test_add_list_dedup_by_film(self):
        f = self._film("第01集")
        edit_queue.add("第01集", f, str(Path(f).parent), (720, 1280), store=self.store, now=1000)
        edit_queue.add("第01集改名", f, str(Path(f).parent), (720, 1280), store=self.store, now=1010)
        items = edit_queue.list_active(store=self.store, now=1020)
        self.assertEqual(len(items), 1)                 # 同一成片只一条
        self.assertEqual(items[0]["title"], "第01集改名")  # 标题被刷新
        self.assertEqual(items[0]["last_active"], 1010)

    def test_ttl_prunes_by_last_active(self):
        f = self._film("旧集")
        edit_queue.add("旧集", f, str(Path(f).parent), store=self.store, now=0)
        # 11h 内还在
        self.assertEqual(len(edit_queue.list_active(store=self.store, now=11 * 3600)), 1)
        # 超过 12h 无操作 → 清除
        self.assertEqual(edit_queue.list_active(store=self.store, now=12 * 3600 + 1), [])

    def test_touch_refreshes_ttl(self):
        f = self._film("续命集")
        edit_queue.add("续命集", f, str(Path(f).parent), store=self.store, now=0)
        item_id = edit_queue.list_active(store=self.store, now=1)[0]["id"]
        # 11h 时点开编辑（touch）→ 刷新，之后再过 11h 仍在
        self.assertTrue(edit_queue.touch(item_id, store=self.store, now=11 * 3600))
        self.assertEqual(len(edit_queue.list_active(store=self.store, now=21 * 3600)), 1)

    def test_missing_film_is_pruned(self):
        f = self._film("会被删")
        edit_queue.add("会被删", f, str(Path(f).parent), store=self.store, now=0)
        Path(f).unlink()                                # 成片文件消失（清缓存/删档）
        self.assertEqual(edit_queue.list_active(store=self.store, now=1), [])

    def test_remove(self):
        f = self._film("手动移除")
        edit_queue.add("手动移除", f, str(Path(f).parent), store=self.store, now=0)
        item_id = edit_queue.list_active(store=self.store, now=1)[0]["id"]
        self.assertTrue(edit_queue.remove(item_id, store=self.store))
        self.assertEqual(edit_queue.list_active(store=self.store, now=1), [])

    def test_concurrent_add_list_no_loss(self):
        # D#1：ThreadingHTTPServer 下并发 add/list 曾会丢条目/清空整队；加锁+原子写后应 0 丢失
        import threading
        films = []
        for i in range(16):
            films.append(_touch_file(self.root / f"c{i}" / "成片.mp4"))
        errs = []

        def work(i):
            try:
                for _ in range(12):
                    edit_queue.add(f"c{i}", films[i], str(Path(films[i]).parent), store=self.store)
                    edit_queue.list_active(store=self.store)
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=work, args=(i,)) for i in range(16)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(edit_queue.list_active(store=self.store)), 16)   # 16 条一条不丢

    def test_stores_and_returns_settings(self):
        f = self._film("带设置")
        edit_queue.add("带设置", f, str(Path(f).parent),
                       settings={"sub": {"size": 88}, "wm": {"enabled": True}},
                       store=self.store, now=1)
        item = edit_queue.list_active(store=self.store, now=2)[0]
        self.assertEqual(item["settings"]["sub"]["size"], 88)
        self.assertTrue(item["settings"]["wm"]["enabled"])

    def test_persists_across_reload(self):
        f = self._film("持久化")
        edit_queue.add("持久化", f, str(Path(f).parent), store=self.store, now=5)
        # 重新读（模拟关软件重开）——数据仍在
        self.assertEqual(len(edit_queue.list_active(store=self.store, now=6)), 1)


if __name__ == "__main__":
    unittest.main()
