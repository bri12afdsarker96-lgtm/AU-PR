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
                  'id="queuePauseBtn"', "无阶段进度"):                        # 暂停按钮 + 心跳看门狗（温和提示）
            self.assertIn(m, html, m)

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
