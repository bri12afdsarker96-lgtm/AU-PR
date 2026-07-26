"""dots.tts 适配器调用签名测试：注入假 dots_tts.runtime + soundfile，验证按 0.2.x 真实 API 调用。

不碰 GPU / 不装真包——只确保导入路径、generate 参数名、返回值落盘三处与上游一致
（用户反馈：module 'dots_tts' has no attribute 'DotsTtsRuntime' 即导入路径错）。
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path

from dub_align_studio.engines import SynthesisOptions
from dub_align_studio.engines.dots_local import DotsLocalEngine
from dub_align_studio.engines.voice_ref import VoiceRef


class _FakeAudio:
    """模拟 torch.Tensor：支持 .float().cpu().squeeze().numpy() 链。"""

    def float(self):
        return self

    def cpu(self):
        return self

    def squeeze(self):
        return self

    def numpy(self):
        return [0.0, 0.1, -0.1]


_MISSING = object()


class _FakeRuntime:
    last_from_pretrained = None
    last_generate = None

    @classmethod
    def from_pretrained(cls, model_name_or_path, **kw):
        cls.last_from_pretrained = {"model": model_name_or_path, **kw}
        return cls()

    def generate(
        self,
        *,
        text,
        prompt_audio_path=None,
        prompt_text=_MISSING,
        num_steps=10,
        guidance_scale=1.2,
        normalize_text=_MISSING,
    ):
        kwargs = {
            "text": text,
            "num_steps": num_steps,
            "guidance_scale": guidance_scale,
        }
        if prompt_audio_path is not None:
            kwargs["prompt_audio_path"] = prompt_audio_path
        if prompt_text is not _MISSING:
            kwargs["prompt_text"] = prompt_text
        if normalize_text is not _MISSING:
            kwargs["normalize_text"] = normalize_text
        _FakeRuntime.last_generate = kwargs
        return {"audio": _FakeAudio(), "sample_rate": 48000}


class DotsAdapterCallSignatureTests(unittest.TestCase):
    def setUp(self):
        _FakeRuntime.last_from_pretrained = None
        _FakeRuntime.last_generate = None
        # 注入假模块：dots_tts（包）+ dots_tts.runtime（含 DotsTtsRuntime）+ soundfile
        self._saved = {n: sys.modules.get(n) for n in ("dots_tts", "dots_tts.runtime", "soundfile")}
        pkg = types.ModuleType("dots_tts")
        runtime_mod = types.ModuleType("dots_tts.runtime")
        runtime_mod.DotsTtsRuntime = _FakeRuntime
        pkg.runtime = runtime_mod
        sf = types.ModuleType("soundfile")
        self.written = {}

        def _write(path, data, sr):
            self.written = {"path": path, "data": data, "sr": sr}
            Path(path).write_bytes(b"RIFFfake")  # 占位文件，供后续存在性检查

        sf.write = _write
        sys.modules["dots_tts"] = pkg
        sys.modules["dots_tts.runtime"] = runtime_mod
        sys.modules["soundfile"] = sf
        # 清运行时缓存，避免跨用例串味
        from dub_align_studio.engines import dots_local
        dots_local._RUNTIME_CACHE.clear()

    def tearDown(self):
        for name, mod in self._saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def test_generate_uses_runtime_submodule_and_correct_kwargs(self):
        engine = DotsLocalEngine(checkpoint="rednote-hilab/dots.tts-soar")
        out = Path(tempfile.mkdtemp()) / "master.wav"
        voice = VoiceRef(voice_id="晚棠", reference_wav=Path("/ref.wav"),
                         transcript="参考句", name="晚棠")
        engine._generate("要合成的整篇文案", voice, out, SynthesisOptions(num_steps=16, guidance_scale=1.5, seed=7))

        # from_pretrained 传了检查点 + 精度
        self.assertEqual(_FakeRuntime.last_from_pretrained["model"], "rednote-hilab/dots.tts-soar")
        self.assertEqual(_FakeRuntime.last_from_pretrained["precision"], "bfloat16")
        # generate 用的是上游参数名；文本前置起音停顿（防丢字），真正文案在其后
        from dub_align_studio.engines import dots_local
        g = _FakeRuntime.last_generate
        self.assertEqual(g["text"], dots_local._ONSET_LEAD_IN + "要合成的整篇文案")
        self.assertTrue(g["text"].endswith("要合成的整篇文案"))
        self.assertEqual(g["prompt_audio_path"], "/ref.wav")   # 不是 prompt_audio
        self.assertEqual(g["prompt_text"], "参考句")
        self.assertEqual(g["num_steps"], 16)
        self.assertEqual(g["guidance_scale"], 1.5)
        # 不得传上游不支持的参数，否则真包会 TypeError
        for bad in ("seed", "speed", "max_pause", "max_pause_seconds", "normalize_text", "prompt_audio"):
            self.assertNotIn(bad, g, bad)
        # 落盘用返回的 sample_rate
        self.assertEqual(self.written["sr"], 48000)
        self.assertTrue(out.exists())

    def test_no_transcript_omits_prompt_text(self):
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp()) / "m.wav"
        voice = VoiceRef(voice_id="v", reference_wav=Path("/r.wav"), transcript="", name="v")
        engine._generate("文案", voice, out, SynthesisOptions())
        self.assertIn("prompt_audio_path", _FakeRuntime.last_generate)
        self.assertNotIn("prompt_text", _FakeRuntime.last_generate)

    def test_normalize_text_passes_only_when_enabled_and_supported(self):
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp()) / "m.wav"
        engine._generate("文案", None, out, SynthesisOptions(normalize_text=True))
        self.assertIs(_FakeRuntime.last_generate["normalize_text"], True)

    def test_runtime_cached_across_calls(self):
        from dub_align_studio.engines import dots_local
        engine = DotsLocalEngine()
        out = Path(tempfile.mkdtemp())
        engine._generate("一", None, out / "a.wav", SynthesisOptions())
        engine._generate("二", None, out / "b.wav", SynthesisOptions())
        self.assertEqual(len(dots_local._RUNTIME_CACHE), 1)  # 只加载一次

    def test_dict_result_missing_audio_errors_clearly(self):
        from dub_align_studio.engines.base import EngineUnavailable
        engine = DotsLocalEngine()
        with self.assertRaises(EngineUnavailable):
            engine._save_result({"sample_rate": 48000}, Path(tempfile.mkdtemp()) / "x.wav")


class OnsetTrimTests(unittest.TestCase):
    """起音丢字兜底：句首停顿 + 落盘前裁掉开头静音的纯逻辑。"""

    def test_trims_leading_silence_keeps_head(self):
        from dub_align_studio.engines.dots_local import _leading_trim_index
        # sr=1000：前 100 样本静音、之后有声；峰值 0.5 → 阈值 0.0075；保留 20ms=20 样本
        seq = [0.0] * 100 + [0.5] * 50
        self.assertEqual(_leading_trim_index(seq, 1000, 0.5), 80)  # 100 - 20 留头

    def test_all_silent_not_trimmed(self):
        from dub_align_studio.engines.dots_local import _leading_trim_index
        self.assertEqual(_leading_trim_index([0.0] * 2000, 1000, 0.0), 0)

    def test_trim_capped_to_avoid_over_cut(self):
        from dub_align_studio.engines.dots_local import _leading_trim_index
        # 首个过阈样本在 2s 处，但封顶 0.8s（=800 样本）内没找到 → 不裁
        seq = [0.0] * 2000 + [0.6] * 10
        self.assertEqual(_leading_trim_index(seq, 1000, 0.6), 0)


class FillerCutTests(unittest.TestCase):
    """牺牲音节切点：嗯+停顿被整体切掉、正文起音保留；无合格缺口时绝不切正文。"""

    def test_cuts_filler_and_gap(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        # sr=1000：50 静音 + 300 填充音 + 200 缺口 + 400 正文
        seq = [0.0]*50 + [0.5]*300 + [0.0]*200 + [0.6]*400
        # 正文起点=样本550（第55窗）；回退 40ms=40样本 → 510
        self.assertEqual(_onset_cut_index(seq, 1000, 0.6), 510)

    def test_no_gap_falls_back_to_silence_trim(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        # 首段发声 0.95s（> 0.6s 填充音上限）→ 不是「嗯」，不切正文，回退普通裁静音 → 0
        seq = [0.5]*950 + [0.0]*200 + [0.6]*300
        self.assertEqual(_onset_cut_index(seq, 1000, 0.6), 0)

    def test_short_pause_after_filler_now_cuts(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        # 用户实测：嗯后只停 60ms（旧 0.12s 门限不达标导致嗯泄漏）→ 现在必须能切
        seq = [0.0]*50 + [0.5]*250 + [0.0]*60 + [0.6]*400
        # 正文起点=样本360；回退 40ms → 320
        self.assertEqual(_onset_cut_index(seq, 1000, 0.6), 320)

    def test_tiny_pause_after_filler_still_cuts(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        # 2026-07-25 根因：模型读完「嗯」常只停几十毫秒，旧 50ms 门限漏切 → 嗯泄漏。
        # 现在与停顿长短无关：哪怕只停 30ms（3 个静音窗）也切到正文起点。
        seq = [0.0]*50 + [0.5]*250 + [0.0]*30 + [0.6]*400
        # 正文起点=样本330（第33窗）；回退 40ms → 290
        self.assertEqual(_onset_cut_index(seq, 1000, 0.6), 290)

    def test_all_silent_untouched(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        self.assertEqual(_onset_cut_index([0.0]*2000, 1000, 0.0), 0)

    def test_long_first_phrase_never_mistaken_for_filler(self):
        from dub_align_studio.engines.dots_local import _onset_cut_index
        # 复刻用户真实音频形态：填充音未产生，正文首句 0.79s + 0.16s 句间停顿——
        # 发声段 0.79s > 0.55s 上限 → 不得当填充音切掉，回退普通裁静音（起点即有声 → 0）
        seq = [0.6]*790 + [0.0]*160 + [0.5]*400
        self.assertEqual(_onset_cut_index(seq, 1000, 0.6), 0)

    def test_lead_in_is_audible_syllable(self):
        from dub_align_studio.engines import dots_local
        # 引子必须含真发音字符（纯标点零音素吸收不了起音不稳——2026-07-24 实测）
        self.assertTrue(any('一' <= ch <= '鿿' for ch in dots_local._ONSET_LEAD_IN))


class TailBurstTests(unittest.TestCase):
    """尾部爆音净化：正文后孤立短爆音删除（余量不得越过爆音起点）；真实短尾字不误删。"""

    def test_isolated_burst_removed_and_keep_capped(self):
        from dub_align_studio.engines.dots_local import _tail_cut_index
        # sr=1000：500 正文 + 90 静音 + 50 爆音 + 190 静音（复刻用户 chunk_003 形态）
        seq = [0.5]*500 + [0.0]*90 + [0.5]*50 + [0.0]*190
        # 正文止 500 + 余量 150 = 650，但封顶爆音起点 590 → 590
        self.assertEqual(_tail_cut_index(seq, 1000, 0.5), 590)

    def test_real_short_tail_word_kept(self):
        from dub_align_studio.engines.dots_local import _tail_cut_index
        # 尾字与正文只隔 40ms（< 60ms 门限）→ 是真话音，不删；止于尾字后+余量
        seq = [0.5]*500 + [0.0]*40 + [0.5]*100 + [0.0]*200
        self.assertEqual(_tail_cut_index(seq, 1000, 0.5), 640+150)

    def test_multiple_bursts_all_removed(self):
        from dub_align_studio.engines.dots_local import _tail_cut_index
        seq = [0.5]*500 + [0.0]*80 + [0.4]*40 + [0.0]*80 + [0.4]*40 + [0.0]*100
        self.assertEqual(_tail_cut_index(seq, 1000, 0.5), 580)  # 封顶最早爆音起点 580

    def test_single_run_never_dropped(self):
        from dub_align_studio.engines.dots_local import _tail_cut_index
        seq = [0.0]*100 + [0.5]*50 + [0.0]*400   # 只有一段发声（哪怕短）→ 保留
        self.assertEqual(_tail_cut_index(seq, 1000, 0.5), 150+150)


class TnStubTests(unittest.TestCase):
    """dots.tts 在导入时硬 import `tn`（WeTextProcessing，靠 pynini，Windows 装不了）。
    缺 tn 时须注入原样返回的桩，让 dots.tts 能导入并出声。"""

    def setUp(self):
        from dub_align_studio.engines import dots_local
        self.dots_local = dots_local
        self._saved = {n: sys.modules.get(n) for n in
                       ("tn", "tn.chinese", "tn.chinese.normalizer",
                        "tn.english", "tn.english.normalizer")}
        for name in self._saved:
            sys.modules.pop(name, None)
        dots_local._TN_STUB_ACTIVE = False

    def tearDown(self):
        for name in ("tn", "tn.chinese", "tn.chinese.normalizer",
                     "tn.english", "tn.english.normalizer"):
            sys.modules.pop(name, None)
        for name, mod in self._saved.items():
            if mod is not None:
                sys.modules[name] = mod
        self.dots_local._TN_STUB_ACTIVE = False

    def test_stub_satisfies_dots_hard_import(self):
        active = self.dots_local._ensure_tn_stub()
        self.assertTrue(active)  # 本环境无真 tn → 应启用桩
        # 复刻 dots_tts/utils/text.py 的硬导入，必须能成功
        from tn.chinese.normalizer import Normalizer as Zh
        from tn.english.normalizer import Normalizer as En
        # 构造函数吃任意参数、normalize 原样返回（跳过正则化不改文本）
        self.assertEqual(Zh(remove_erhua=True, cache_dir="x").normalize("测试123"), "测试123")
        self.assertEqual(En().normalize("abc"), "abc")

    def test_real_tn_not_overridden(self):
        fake_tn = types.ModuleType("tn")
        sys.modules["tn"] = fake_tn  # 冒充真 tn 已在
        self.assertFalse(self.dots_local._ensure_tn_stub())  # 有真 tn → 不注入桩
        self.assertIs(sys.modules["tn"], fake_tn)


class DotsEmptyRefTests(unittest.TestCase):
    """D#5：参考音频路径为空时不能变成 "." （指向 cwd）；应走引擎自带声线不带 prompt。"""

    def test_empty_reference_wav_not_dot(self):
        from dub_align_studio.engines.dots_local import DotsLocalEngine
        captured = {}

        class RT:
            @classmethod
            def from_pretrained(cls, *a, **k): return cls()
            def generate(self, **kw): captured.update(kw); return {"audio": _FakeAudio(), "sample_rate": 48000}

        eng = DotsLocalEngine(checkpoint="x")
        eng._load_runtime = lambda: RT()          # type: ignore[assignment]
        eng._save_result = staticmethod(lambda *a, **k: None)
        v = VoiceRef(voice_id="空参考", reference_wav=Path(""), transcript="", name="空参考")
        eng._generate("一句", v, Path(tempfile.mkdtemp()) / "m.wav", SynthesisOptions())
        self.assertNotIn("prompt_audio_path", captured)   # 空参考 → 不传 prompt（不会是 "."）


class _LongRefRuntime:
    """模拟 dots.tts：参考音频 patch=870，max_generate_length 需 > 870 才成功。"""
    PATCH = 870
    calls = []

    @classmethod
    def from_pretrained(cls, model_name_or_path, **kw):
        return cls()

    def generate(self, **kwargs):
        mgl = int(kwargs.get("max_generate_length") or 500)
        _LongRefRuntime.calls.append(mgl)
        if mgl <= self.PATCH:
            raise ValueError(
                "max_generate_length must exceed prompt audio patch count when prompt_text "
                f"is provided: max_generate_length={mgl} prompt_audio_patch_count={self.PATCH}.")
        return {"audio": _FakeAudio(), "sample_rate": 48000}


class DotsLongReferenceRetryTests(unittest.TestCase):
    """长参考音频（patch 870 > 默认 500）：应解析报错里的 patch 数、抬高 max_generate_length
    精确重试成功，并对同一参考缓存、后续行不再先失败。"""

    def setUp(self):
        _LongRefRuntime.calls = []
        self._saved = {n: sys.modules.get(n) for n in ("dots_tts", "dots_tts.runtime", "soundfile")}
        pkg = types.ModuleType("dots_tts"); rt = types.ModuleType("dots_tts.runtime")
        rt.DotsTtsRuntime = _LongRefRuntime; pkg.runtime = rt
        sf = types.ModuleType("soundfile"); sf.write = lambda p, d, s: Path(p).write_bytes(b"RIFFfake")
        sys.modules.update({"dots_tts": pkg, "dots_tts.runtime": rt, "soundfile": sf})
        from dub_align_studio.engines import dots_local
        dots_local._RUNTIME_CACHE.clear(); dots_local._MGL_CACHE.clear()
        self.dots_local = dots_local

    def tearDown(self):
        for n, m in self._saved.items():
            if m is None: sys.modules.pop(n, None)
            else: sys.modules[n] = m

    def test_retries_with_patch_count_and_caches(self):
        engine = DotsLocalEngine(checkpoint="x")
        base = Path(tempfile.mkdtemp())
        voice = VoiceRef(voice_id="长参考", reference_wav=base / "ref.wav", transcript="参考", name="长参考")
        # 第一行：先默认失败 → 解析 870 → 用 870+预算 重试成功
        engine._generate("第一句", voice, base / "1.wav", SynthesisOptions())
        self.assertGreater(_LongRefRuntime.calls[-1], _LongRefRuntime.PATCH)  # 重试值 > patch 数
        self.assertTrue((base / "1.wav").exists())
        first_calls = len(_LongRefRuntime.calls)
        # 第二行：应直接带缓存的 max_generate_length 一次成功（不再先失败）
        engine._generate("第二句", voice, base / "2.wav", SynthesisOptions())
        self.assertEqual(len(_LongRefRuntime.calls), first_calls + 1)  # 只多一次调用=没有 fail+retry
        self.assertGreater(_LongRefRuntime.calls[-1], _LongRefRuntime.PATCH)


if __name__ == "__main__":
    unittest.main()
