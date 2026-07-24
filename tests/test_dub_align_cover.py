"""封面工厂 UI 契约测试：页签接线 / 图层面板（PS 习惯）/ 模板 / 导出 / 字体同步。

交互行为（拖拽/命中/键盘）由 Playwright 烟测覆盖；这里锁定单文件 HTML 的接线不被回归破坏。
"""

import unittest
from pathlib import Path

from dub_align_studio import web_server


def _html() -> str:
    return (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")


class CoverStudioContractTests(unittest.TestCase):
    def test_nav_between_textbox_and_voices(self):
        html = _html()
        p2 = html.index('data-p="p2"')
        p6 = html.index('data-p="p6"')
        p3 = html.index('data-p="p3"')
        self.assertTrue(p2 < p6 < p3, "封面工厂页签必须位于 文本框 与 音色库 之间")

    def test_core_wiring(self):
        html = _html()
        for marker in (
            'id="cvCanvas"', "cvAddText", "cvImportImg",      # 画布 + 文字层 + 底图导入
            "cvRenderLayers", "背景图层",                      # 图层面板 + 底图即图层
            "cvExport", 'id="cvW"', 'id="cvH"', 'id="cvPreset"',  # 自定义像素导出
            "cvTplSave", "cvTplLoad", "cvTplRename", "cvTplDel", "dubCoverTpl",  # 模板
            'id="cvFont"', "fillGrouped('cvFont'",             # 字体同步字体库
            "cvGrid9",                                         # 九宫格快捷定位
        ):
            self.assertIn(marker, html, marker)

    def test_ps_layer_panel_behaviors(self):
        html = _html()
        # 👁显隐 / 🔒锁定（可选中不可动、不可删）/ 拖拽排序 / 双击重命名
        for marker in ("L.visible=!L.visible", "L.locked=!L.locked",
                       "draggable=true", "ondblclick", "ondragstart", "ondrop",
                       "if(L.locked)return; // 与 PS 一致：锁定层不可删"):
            self.assertIn(marker, html, marker)

    def test_align_and_free_position(self):
        html = _html()
        for marker in ("'align','left'", "'align','center'", "'align','right'",
                       'id="cvX"', 'id="cvY"'):  # 左中右对齐 + 百分比精确定位
            self.assertIn(marker, html, marker)


if __name__ == "__main__":
    unittest.main()
