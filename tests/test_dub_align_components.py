"""工具箱组件商店测试：清单/状态纯逻辑 + 未知键与指引路径（不做真实网络下载）。"""

import unittest

from dub_align_studio import components


class ComponentCatalogTests(unittest.TestCase):
    def test_catalog_covers_all_kinds(self):
        kinds = {c["kind"] for c in components.COMPONENTS}
        self.assertEqual(kinds, {"download", "pip", "install", "torch"})  # torch=GPU运行时一键装

    def test_torch_before_dots_and_uses_cuda_index(self):
        keys = [c["key"] for c in components.COMPONENTS]
        self.assertIn("torch_cuda", keys)
        self.assertLess(keys.index("torch_cuda"), keys.index("dots_tts"))  # 先装 torch 再装 dots
        self.assertIn("download.pytorch.org/whl/cu121", components.TORCH_CUDA_INDEX)
        st = {c["key"]: c for c in components.component_statuses()}
        self.assertIn("torch_cuda", st)
        self.assertTrue(st["torch_cuda"]["detail"])
        keys = [c["key"] for c in components.COMPONENTS]
        self.assertEqual(len(keys), len(set(keys)))
        for item in components.COMPONENTS:
            self.assertTrue(item["name"] and item["purpose"], item["key"])

    def test_statuses_shape_and_detail(self):
        statuses = components.component_statuses()
        self.assertEqual(len(statuses), len(components.COMPONENTS))
        for status in statuses:
            self.assertIn("installed", status)
            self.assertTrue(status["detail"], status["key"])

    def test_download_components_report_missing_in_clean_env(self):
        statuses = {s["key"]: s for s in components.component_statuses()}
        # 本容器无 vendor_tools：whisper 组件应为未就绪且给出指引
        if not statuses["whisper_cli"]["installed"]:
            self.assertTrue(statuses["whisper_cli"]["detail"])
        if not statuses["dots_tts"]["installed"]:
            self.assertIn("pip install", statuses["dots_tts"]["detail"])


class InstallGuardTests(unittest.TestCase):
    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            components.install_component("不存在", lambda _m: None)

    def test_fish_mirror_urls_cover_source(self):
        urls = components._gh_mirror_urls(components.FISH_SOURCE_URLS)
        self.assertTrue(any(u.startswith("https://ghproxy.net/") for u in urls))
        self.assertTrue(any(u.startswith("https://ghfast.top/") for u in urls))
        self.assertIn(components.FISH_SOURCE_URLS[0], urls)  # 原始直连保底

    def test_pip_installed_detection(self):
        self.assertTrue(components._pip_installed("json"))  # 标准库恒可导入
        self.assertFalse(components._pip_installed("绝对不存在的包名xyz"))


if __name__ == "__main__":
    unittest.main()
