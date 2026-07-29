"""第八轮迭代 UI 契约：色号删除 / 文本框样式模板 / 素材库管理 / 文本框页重排 / 打包自检。"""

import unittest
from pathlib import Path

from dub_align_studio import web_server


class UiContractV8Tests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    def test_color_delete_wired(self):
        self.assertIn("delColor", self.html)         # 自定义色号删除
        self.assertIn("★ 存色", self.html)

    def test_overlay_template_wired(self):
        for m in ('id="tplSel"', "saveTpl", "applyTpl", "renameTpl", "delTpl", "curStyle", "样式模板"):
            self.assertIn(m, self.html, m)

    def test_asset_library_manager_wired(self):
        for m in ('id="assetList"', "renderAssetList", "delAsset", "toggleAssets", "素材库"):
            self.assertIn(m, self.html, m)

    def test_p2_layout_three_columns(self):
        # 编辑文本框为独立主列；右列堆叠（2026-07-26：字幕样式移到一键成片，右列改为待编辑队列 + 已添加）
        self.assertIn("主列：编辑文本框", self.html)
        self.assertIn("右列：待编辑队列 + 已添加", self.html)
        # 已添加表格去掉时间窗列后为 5 列（4 表头 + 操作列）
        self.assertIn('colspan="5"', self.html)


class PackagingScriptTests(unittest.TestCase):
    def _bat(self) -> str:
        root = Path(__file__).resolve().parents[1]
        for name in ("项目打包.bat", "打包.bat"):  # 新名优先，兼容旧名
            for cand in (Path(name), root / name):
                if cand.exists():
                    return cand.read_text(encoding="utf-8")
        self.skipTest("项目打包.bat / 打包.bat 均不在此仓库根")

    def test_cleans_stale_and_selfchecks(self):
        bat = self._bat()
        self.assertIn("__pycache__", bat)      # 清旧字节码
        self.assertIn(":SELFFAIL", bat)         # 自检失败分支（收尾脚本非0退出触发）
        self.assertIn("打包收尾.py", bat)        # 自检/版本/压缩包收尾在独立脚本

    def test_calls_finalize_script(self):
        bat = self._bat()
        # 收尾改用独立脚本，避免 cmd 对 python -c 复杂引号拆断
        self.assertIn("打包收尾.py", bat)
        self.assertNotIn("python -c", bat)     # 不再在 bat 里用 python -c

    def test_finalize_script_present_and_valid(self):
        for cand in (Path("打包收尾.py"), Path(__file__).resolve().parents[1] / "打包收尾.py"):
            if cand.exists():
                src = cand.read_text(encoding="utf-8")
                self.assertIn("make_archive", src)   # 生成压缩包
                self.assertIn("发布包", src)          # 输出目录
                self.assertIn("APP_VERSION", src)    # 文件名含版本号
                self.assertIn("版本.txt", src)        # 写版本文件
                compile(src, str(cand), "exec")      # 语法可编译
                return
        self.skipTest("打包收尾.py 不在此仓库根")


class VersionedZipLogicTests(unittest.TestCase):
    def test_archive_names_with_version_and_contains_product(self):
        import datetime
        import shutil
        import tempfile
        import zipfile
        from pathlib import Path

        from dub_align_studio import version

        work = Path(tempfile.mkdtemp(prefix="zip_"))
        try:
            prod = work / "dist" / "水星配音对齐工作室"
            prod.mkdir(parents=True)
            (prod / "水星配音对齐工作室.exe").write_text("x", encoding="utf-8")
            name = ("水星配音对齐工作室_v" + version.APP_VERSION + "_" +
                    datetime.datetime(2026, 7, 23, 15, 30).strftime("%Y%m%d_%H%M"))
            (work / "发布包").mkdir()
            archive = shutil.make_archive(str(work / "发布包" / name), "zip",
                                          str(work / "dist"), "水星配音对齐工作室")
            self.assertTrue(archive.endswith(".zip"))
            self.assertIn(version.APP_VERSION, Path(archive).name)  # 文件名含版本号 → 可区分
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
            self.assertIn("水星配音对齐工作室/水星配音对齐工作室.exe", names)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
