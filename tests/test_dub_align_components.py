"""工具箱组件商店测试：清单/状态纯逻辑 + 未知键与指引路径（不做真实网络下载）。"""

import unittest

from dub_align_studio import components


class ComponentCatalogTests(unittest.TestCase):
    def test_catalog_covers_all_kinds(self):
        kinds = {c["kind"] for c in components.COMPONENTS}
        self.assertEqual(kinds, {"download", "pip", "install", "torch", "dots"})

    def test_dots_install_skips_pynini_wetextprocessing(self):
        # dots.tts 运行依赖里绝不能含 WeTextProcessing/pynini（Windows 编译不了），
        # 也不含 torch（避免覆盖 CUDA 版）
        # 取每项的包名（去掉 ==版本 / [extra]），精确判断
        def pkg(dep):
            return dep.split("==")[0].split("[")[0].strip().lower()

        names = [pkg(d) for d in components.DOTS_RUNTIME_DEPS]
        for banned in ("wetextprocessing", "pynini", "torch", "torchaudio"):
            self.assertNotIn(banned, names, banned)  # torchdiffeq 是独立名，不会误判
        for need in ("loguru", "transformers", "soundfile", "accelerate"):
            self.assertIn(need, names, need)
        # transformers 必须锁到有 Qwen2 支持的版本
        self.assertIn("transformers==4.57.0", components.DOTS_RUNTIME_DEPS)

    def test_torch_before_dots_and_uses_cuda_index(self):
        keys = [c["key"] for c in components.COMPONENTS]
        self.assertIn("torch_cuda", keys)
        self.assertLess(keys.index("torch_cuda"), keys.index("dots_tts"))  # 先装 torch 再装 dots
        # 必须是 cu126：cu121 源停更在 torch 2.5.1，会把新版 torch 降级装坏（2026-07-24 实测）
        self.assertIn("download.pytorch.org/whl/cu126", components.TORCH_CUDA_INDEX)
        # torchvision↔torch 配对判断（0.n+15 ↔ 2.n）
        self.assertTrue(components._torchvision_pairs_with("0.26.0", "2.11.0"))
        self.assertFalse(components._torchvision_pairs_with("0.26.0", "2.5.1"))
        self.assertTrue(components._torchvision_pairs_with("坏版本", "2.11.0"))  # 解析失败→保守不动
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
            self.assertIn("安装", statuses["dots_tts"]["detail"])  # dots 走 Windows 安全装法


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


class CloudOnlyModeTests(unittest.TestCase):
    """轻量云配版：隐藏本地模型组件/引擎，不影响正式版。"""

    def test_cloud_only_hides_local_model_components(self):
        import os

        from dub_align_studio import settings

        os.environ["MERCURY_CLOUD_ONLY"] = "1"
        try:
            self.assertTrue(settings.cloud_only())
            keys = {c["key"] for c in components.component_statuses()}
            self.assertFalse({"torch_cuda", "dots_tts", "fish_speech"} & keys, "云配版不应出现本地模型组件")
            self.assertLessEqual({"whisper_cli", "small", "pycapcut"}, keys, "whisper/pycapcut 应保留")
        finally:
            os.environ.pop("MERCURY_CLOUD_ONLY", None)
        # 正式版(未设环境变量)照旧含 dots_tts
        self.assertFalse(settings.cloud_only())
        self.assertIn("dots_tts", {c["key"] for c in components.component_statuses()})


if __name__ == "__main__":
    unittest.main()
