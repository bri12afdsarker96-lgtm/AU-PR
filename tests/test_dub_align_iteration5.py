"""第五轮迭代测试：下载源修死链（google/fonts 换源 + 霞鹜 Medium）/ 手动兜底指引 / 预览滑杆契约。

背景（用户 2026-07-22 截图）：马善政/龙藏/指尖芒星/刘建毛草/站酷小薇/庆科黄油 全部
下载失败——原 googlefonts/<单仓> 路径不存在（404）；霞鹜文楷「粗」在 v1.510 无
Bold 资产。以上均已核实（GitHub 目录/release 资产清单），换为核实存在的源。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from dub_align_studio import components, fonts, settings as studio_settings

# 已核实存在的 google/fonts 路径（2026-07-22 逐一验证目录与文件名）
GOOGLE_FONTS_KEYS = {
    "zcool_xiaowei": "ofl/zcoolxiaowei/ZCOOLXiaoWei-Regular.ttf",
    "zcool_qingke": "ofl/zcoolqingkehuangyou/ZCOOLQingKeHuangYou-Regular.ttf",
    "ma_shan_zheng": "ofl/mashanzheng/MaShanZheng-Regular.ttf",
    "long_cang": "ofl/longcang/LongCang-Regular.ttf",
    "zhi_mang_xing": "ofl/zhimangxing/ZhiMangXing-Regular.ttf",
    "liu_jian_mao_cao": "ofl/liujianmaocao/LiuJianMaoCao-Regular.ttf",
}


class FontSourceFixTests(unittest.TestCase):
    def _pack(self) -> dict:
        return {f["key"]: f for f in fonts.FONT_PACK}

    def test_dead_google_repos_replaced_with_google_fonts(self):
        pack = self._pack()
        for key, path in GOOGLE_FONTS_KEYS.items():
            url = str(pack[key]["url"])
            self.assertIn("github.com/google/fonts/raw/main/" + path, url, key)

    def test_lxgw_bold_replaced_with_medium(self):
        item = self._pack()["lxgw_wenkai_bold"]
        self.assertEqual(item["file"], "LXGWWenKai-Medium.ttf")  # v1.510 无 Bold 资产
        self.assertIn("LXGWWenKai-Medium.ttf", str(item["url"]))
        self.assertNotIn("Bold", str(item["url"]))

    def test_no_font_points_at_nonexistent_single_repos(self):
        # 已核实不存在的旧路径不允许回归
        dead_markers = ["googlefonts/zcoolxiaowei", "googlefonts/zcool-qingke-huangyou",
                        "googlefonts/ma-shan-zheng", "googlefonts/long-cang",
                        "googlefonts/zhi-mang-xing", "googlefonts/liu-jian-mao-cao"]
        for item in fonts.FONT_PACK:
            for marker in dead_markers:
                self.assertNotIn(marker, str(item["url"]), item["key"])


class ManualFallbackTests(unittest.TestCase):
    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.temp = Path(tempfile.mkdtemp(prefix="fallback_"))
        studio_settings.SETTINGS_FILE = self.temp / "settings.json"
        studio_settings.set_data_root(self.temp / "数据")

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_font_install_failure_gives_manual_guide(self):
        import integrated_workbench.component_download as cd

        original = cd.download_verified_file

        def boom(*_args, **_kwargs):
            raise RuntimeError("全部源不可达")

        cd.download_verified_file = boom
        try:
            with self.assertRaises(RuntimeError) as ctx:
                fonts.install_font("ma_shan_zheng", lambda _m: None)
        finally:
            cd.download_verified_file = original
        message = str(ctx.exception)
        self.assertIn("手动兜底", message)
        self.assertIn("MaShanZheng-Regular.ttf", message)
        self.assertIn(str(studio_settings.fonts_dir()), message)

    def test_component_manual_hint_contains_url_and_target(self):
        entry = {"urls": ["https://github.com/x/y/releases/z.zip"]}
        hint = components._manual_download_hint(entry, Path("/tmp/组件/z.zip"), RuntimeError("超时"))
        self.assertIn("https://github.com/x/y/releases/z.zip", hint)
        self.assertIn("再点一次「下载」", hint)


class WhisperCliLayoutTests(unittest.TestCase):
    """官方 whisper-bin-x64.zip 内含一层 Release\\ 目录（v1.9.1 打包命令核实）——
    解压后必须能定位 whisper-cli.exe（用户 2026-07-22 报「解压后仍未找到」）。"""

    def setUp(self):
        self._backup = studio_settings.SETTINGS_FILE
        self.temp = Path(tempfile.mkdtemp(prefix="wcli_"))
        studio_settings.SETTINGS_FILE = self.temp / "settings.json"
        studio_settings.set_data_root(self.temp / "数据")

    def tearDown(self):
        studio_settings.SETTINGS_FILE = self._backup
        shutil.rmtree(self.temp, ignore_errors=True)

    def _place(self, relative: str) -> Path:
        target = studio_settings.whisper_home() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"MZ")
        return target

    def test_official_zip_release_layout_found(self):
        exe = self._place("Release/whisper-cli.exe")
        self.assertEqual(studio_settings.whisper_cli_path(), exe)

    def test_legacy_build_layout_still_first(self):
        legacy = self._place("build/bin/Release/whisper-cli.exe")
        self._place("Release/whisper-cli.exe")
        self.assertEqual(studio_settings.whisper_cli_path(), legacy)

    def test_unknown_nested_layout_found_recursively(self):
        exe = self._place("某未来版本/bin/whisper-cli.exe")
        self.assertEqual(studio_settings.whisper_cli_path(), exe)


class VersionTests(unittest.TestCase):
    def test_full_version_shape(self):
        from dub_align_studio import version

        text = version.full_version()
        self.assertTrue(text.startswith(f"v{version.APP_VERSION}"), text)
        # 源码直跑（无 _build_info）时明确标注，与打包产物可区分
        if not version.build_stamp():
            self.assertIn("源码运行", text)

    def test_state_endpoint_exposes_version(self):
        from dub_align_studio.web_server import _state_payload

        payload = _state_payload()
        self.assertTrue(str(payload.get("version", "")).startswith("v"), payload.get("version"))


class UiPreviewContractTests(unittest.TestCase):
    def test_preview_slider_is_percent_based(self):
        html = (Path(fonts.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="pvScale" min="40" max="100"', html)  # 百分比滑杆全程有效
        self.assertIn("pvScaleV", html)
        self.assertIn("clientWidth", html)  # 预览宽度按预览卡实际宽度换算
        self.assertIn("max-width:1720px", html)

    def test_version_badge_wired(self):
        html = (Path(fonts.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="verFlag"', html)
        self.assertIn('id="verFoot"', html)


if __name__ == "__main__":
    unittest.main()
