"""素材调度测试：文件夹顺序/关键字匹配/夹内 Seed 选片/兜底/清单复用（全确定性，不碰渲染）。"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio import material_select as ms
from dub_align_studio import studio_pipeline as pipeline


def _mk(root: Path, folders: dict[str, int]) -> None:
    """建素材树：{文件夹名: 视频个数}。"""
    for name, count in folders.items():
        d = root / name
        d.mkdir(parents=True)
        for i in range(1, count + 1):
            (d / f"v{i}.mp4").write_bytes(b"x")


class ScoreTests(unittest.TestCase):
    def test_full_name_hit_is_strongest(self):
        self.assertEqual(ms.score_folder_name("01健身房", "他每天在健身房撸铁"), 1.0)

    def test_partial_bigram_hit(self):
        s = ms.score_folder_name("健身房撸铁", "他走进健身房")   # 部分二元组命中
        self.assertTrue(0 < s < 1.0, s)

    def test_no_hit_and_junk_names(self):
        self.assertEqual(ms.score_folder_name("仓库搬货", "他在健身房锻炼"), 0.0)
        self.assertEqual(ms.score_folder_name("01", "任何文案"), 0.0)  # 清洗后为空

    def test_natural_folder_order(self):
        work = Path(tempfile.mkdtemp(prefix="mat_"))
        try:
            _mk(work, {"10仓库": 1, "2街头": 1, "01健身房": 1})
            names = [f.name for f in ms.list_material_folders(work)]
            self.assertEqual(names, ["01健身房", "2街头", "10仓库"])  # 数值序
        finally:
            shutil.rmtree(work, ignore_errors=True)


class SelectTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="sel_"))
        _mk(self.work, {"01健身房": 2, "02街头": 1, "03仓库": 3})

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_folder_order_cycles_with_note(self):
        lines = [f"第{i}句" for i in range(1, 6)]  # 5 行 3 夹 → 循环
        shots = ms.select_videos(self.work, lines, "folder_order", seed=42)
        self.assertEqual([s.folder for s in shots],
                         ["01健身房", "02街头", "03仓库", "01健身房", "02街头"])
        self.assertEqual(shots[3].note, "循环复用")
        self.assertEqual(shots[0].note, "")

    def test_keyword_picks_best_folder_and_falls_back(self):
        lines = ["他在健身房挥汗如雨", "深夜的街头空无一人", "这句谁都匹配不上呀"]
        shots = ms.select_videos(self.work, lines, "keyword", seed=42)
        self.assertEqual(shots[0].folder, "01健身房")
        self.assertIn("匹配「健身房」", shots[0].note)
        self.assertEqual(shots[1].folder, "02街头")
        self.assertEqual(shots[2].folder, "03仓库")  # 第 3 行兜底 → 第 3 夹
        self.assertIn("兜底", shots[2].note)

    def test_seed_deterministic_and_no_repeat_until_exhausted(self):
        lines = ["健身房一", "健身房二", "健身房三"]  # 三行都匹配 01健身房（2 个视频）
        a = ms.select_videos(self.work, lines, "keyword", seed=7)
        b = ms.select_videos(self.work, lines, "keyword", seed=7)
        self.assertEqual([s.file for s in a], [s.file for s in b])       # 同 Seed 可复现
        self.assertNotEqual(a[0].file, a[1].file)                        # 夹内 2 片先取不同
        self.assertIn(a[2].file, {a[0].file, a[1].file})                 # 用完循环

    def test_empty_folder_and_no_subfolders_error(self):
        empty = Path(tempfile.mkdtemp(prefix="e_"))
        try:
            with self.assertRaises(ValueError):
                ms.select_videos(empty, ["一句"], "folder_order")
            (empty / "空夹").mkdir()
            with self.assertRaises(ValueError):
                ms.select_videos(empty, ["一句"], "folder_order")
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_selection_csv_roundtrip_and_reuse(self):
        out = Path(tempfile.mkdtemp(prefix="out_"))
        try:
            lines = ["健身房", "街头"]
            shots = ms.select_videos(self.work, lines, "keyword", seed=1)
            ms.write_selection(out, shots)
            reused = ms.read_selection(out, expect_lines=2)
            self.assertEqual(reused, [s.file for s in shots])
            self.assertIsNone(ms.read_selection(out, expect_lines=3))   # 行数不一致 → 重选
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_pipeline_select_reuses_manifest(self):
        out = Path(tempfile.mkdtemp(prefix="pout_"))
        try:
            lines = ["健身房", "街头"]
            first = pipeline.select_shot_videos(self.work, lines, "keyword", 42, out)
            logs: list[str] = []
            second = pipeline.select_shot_videos(self.work, lines, "keyword", 99, out, log=logs.append)
            self.assertEqual(first, second)                              # 复用清单，Seed 变了也不换画面
            self.assertTrue(any("复用" in m for m in logs))
        finally:
            shutil.rmtree(out, ignore_errors=True)


class FlatShotListTests(unittest.TestCase):
    """flat 模式（输出目录默认=分镜目录）：上一轮生成的 成片.mp4 不能被当成一个分镜。"""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="flat_"))

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_generated_film_excluded_from_shot_list(self):
        for i in (1, 2, 3):
            (self.work / f"{i}.mp4").write_bytes(b"x")
        (self.work / pipeline.FILM_NAME).write_bytes(b"film")   # 上一轮渲染留下的成片
        vids = pipeline.list_shot_videos(self.work)
        self.assertEqual([p.name for p in vids], ["1.mp4", "2.mp4", "3.mp4"])
        self.assertNotIn(pipeline.FILM_NAME, [p.name for p in vids])

    def test_missing_shot_not_masked_by_film(self):
        # 只有 1.mp4、2.mp4 两个真分镜，外加成片；3 行文案应报「不足」而非把成片选给第 3 行
        (self.work / "1.mp4").write_bytes(b"x")
        (self.work / "2.mp4").write_bytes(b"x")
        (self.work / pipeline.FILM_NAME).write_bytes(b"film")
        with self.assertRaises(ValueError):
            pipeline.select_shot_videos(self.work, ["甲", "乙", "丙"], "flat", 42, self.work)


class UiContractTests(unittest.TestCase):
    def test_material_mode_wired(self):
        from dub_align_studio import web_server
        html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        for marker in ('id="matMode"', "material_mode", "folder_order", "keyword", "选片清单"):
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
