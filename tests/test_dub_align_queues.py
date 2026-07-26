"""待编辑队列（⑧）与生成任务队列（⑨）测试。"""

import tempfile
import unittest
from pathlib import Path

from dub_align_studio import edit_queue


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

    def test_persists_across_reload(self):
        f = self._film("持久化")
        edit_queue.add("持久化", f, str(Path(f).parent), store=self.store, now=5)
        # 重新读（模拟关软件重开）——数据仍在
        self.assertEqual(len(edit_queue.list_active(store=self.store, now=6)), 1)


if __name__ == "__main__":
    unittest.main()
