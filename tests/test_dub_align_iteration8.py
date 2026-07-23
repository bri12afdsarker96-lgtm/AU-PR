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
        # 编辑文本框为独立主列；字幕样式 + 已添加 堆叠在同一（右）列
        self.assertIn("主列：编辑文本框", self.html)
        self.assertIn("右列：字幕样式 + 已添加", self.html)
        # 已添加表格去掉时间窗列后为 5 列（4 表头 + 操作列）
        self.assertIn('colspan="5"', self.html)


class PackagingScriptTests(unittest.TestCase):
    def _bat(self) -> str:
        for cand in (Path("打包.bat"), Path(__file__).resolve().parents[1] / "打包.bat"):
            if cand.exists():
                return cand.read_text(encoding="utf-8")
        self.skipTest("打包.bat 不在此仓库根")

    def test_cleans_stale_and_selfchecks(self):
        bat = self._bat()
        self.assertIn("__pycache__", bat)      # 清旧字节码
        self.assertIn("[自检]", bat)            # 自行检测
        self.assertIn(":SELFFAIL", bat)         # 自检失败分支
        self.assertIn("版本.txt", bat)


if __name__ == "__main__":
    unittest.main()
