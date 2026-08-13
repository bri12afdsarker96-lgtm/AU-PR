"""WAV 后处理层专项测试：把"speed/max_pause 是不是真进入了产物"钉死。

覆盖：
    · atempo_chain 分解多级值（含 speed>1 / <1 / 极大 / 极小 / 近似 1）
    · postprocess_wav：真跑 ffmpeg（若可用）时 speed=2 时长约减半、0.5 时长约翻倍；
      max_pause=0 不改变静音；max_pause=0.2 能把长静音压到 ~0.2 秒。
    · Edge 契约：native_speed=True + speed=2 → **不做**变速（防双倍）；
    · 失败保留旧 output：模拟 ffmpeg 失败时保留旧文件、清临时。
    · engines/longform 的每段后处理钩子对 mock/dots_local 引擎调用；
      Edge TTS（native_speed=True）时跳过 atempo。

ffmpeg 缺失时**跳过**需要 ffmpeg 的用例，纯逻辑用例仍全跑（allow skip 明确原因）。
"""

from __future__ import annotations

import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from dub_align_studio.engines import audio_postprocess as apo
from dub_align_studio.engines import longform, mock_engine
from dub_align_studio.engines.base import EngineCapabilities, SynthesisOptions


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _write_tone(path: Path, seconds: float, freq: float = 440.0, sample_rate: int = 22050) -> None:
    """写一段确定性正弦音："音调"部分，供 atempo 时长验证。"""
    n = int(sample_rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * freq * i / sample_rate))
            w.writeframes(struct.pack("<h", v))


def _write_tone_silence_tone(path: Path, tone_seconds: float, silence_seconds: float,
                              sample_rate: int = 22050) -> None:
    """"音调—长静音—音调"三段测试音，用于 max_pause 静音压缩。"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(int(sample_rate * tone_seconds)):
            v = int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            w.writeframes(struct.pack("<h", v))
        w.writeframes(b"\x00\x00" * int(sample_rate * silence_seconds))
        for i in range(int(sample_rate * tone_seconds)):
            v = int(12000 * math.sin(2 * math.pi * 660 * i / sample_rate))
            w.writeframes(struct.pack("<h", v))


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class AtempoChainTests(unittest.TestCase):
    """纯字符串逻辑：atempo 值分解，不依赖 ffmpeg。"""

    def test_identity(self):
        self.assertEqual(apo.atempo_chain(1.0), [])
        self.assertEqual(apo.atempo_chain(1.0 + 1e-6), [])  # 近似 1

    def test_zero_and_negative_raise(self):
        """P0-3：speed<=0 明确报错，不再静默返回空链——防止用户把 0 当 1.0 走的假成功。"""
        with self.assertRaises(ValueError):
            apo.atempo_chain(0)
        with self.assertRaises(ValueError):
            apo.atempo_chain(-1)
        with self.assertRaises(ValueError):
            apo.atempo_chain(float("nan"))

    def test_postprocess_wav_rejects_bad_speed(self):
        """P0-3：postprocess_wav 入口层同样拒绝 0/负数/非数字，抛 PostProcessError。"""
        with self.assertRaises(apo.PostProcessError):
            apo.postprocess_wav(Path("/tmp/nope.wav"), speed=0.0,
                                 max_pause_seconds=0.0, native_speed=False)
        with self.assertRaises(apo.PostProcessError):
            apo.postprocess_wav(Path("/tmp/nope.wav"), speed=-1.5,
                                 max_pause_seconds=0.0, native_speed=False)

    def test_single_node_speeds(self):
        self.assertEqual(apo.atempo_chain(2.0), ["2.000000"])
        self.assertEqual(apo.atempo_chain(0.5), ["0.500000"])
        self.assertEqual(apo.atempo_chain(1.5), ["1.500000"])

    def test_multi_node_when_out_of_range(self):
        chain = apo.atempo_chain(4.0)   # ffmpeg 单节点范围 0.5~100；4.0 单节即可
        self.assertEqual(chain, ["4.000000"])
        chain = apo.atempo_chain(200.0)  # 超过 100 → 拆成多节
        self.assertGreaterEqual(len(chain), 2)
        product = 1.0
        for v in chain:
            product *= float(v)
        self.assertAlmostEqual(product, 200.0, places=3)
        chain = apo.atempo_chain(0.1)  # 低于 0.5 → 拆
        product = 1.0
        for v in chain:
            product *= float(v)
        self.assertAlmostEqual(product, 0.1, places=3)


@unittest.skipUnless(_ffmpeg_available(), "需要 ffmpeg 才能实测 atempo 时长")
class PostprocessAtempoTimingTests(unittest.TestCase):
    """真跑 ffmpeg：speed=2 时长约减半、0.5 时长约翻倍——业务契约锁死。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo_atempo_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self) -> Path:
        p = self.tmp / "in.wav"
        _write_tone(p, seconds=2.0)   # 2 秒
        return p

    def test_speed_2_halves_duration(self):
        p = self._prepare()
        actual = apo.postprocess_wav(p, speed=2.0, max_pause_seconds=0.0, native_speed=False)
        self.assertAlmostEqual(actual, 1.0, delta=0.15)   # 允许编码/取整偏差

    def test_speed_0_5_doubles_duration(self):
        p = self._prepare()
        actual = apo.postprocess_wav(p, speed=0.5, max_pause_seconds=0.0, native_speed=False)
        self.assertAlmostEqual(actual, 4.0, delta=0.20)

    def test_speed_1_no_op(self):
        p = self._prepare()
        before = _wav_seconds(p)
        actual = apo.postprocess_wav(p, speed=1.0, max_pause_seconds=0.0, native_speed=False)
        self.assertAlmostEqual(actual, before, delta=0.05)

    def test_native_speed_engine_not_processed(self):
        """Edge 契约：native_speed=True 时无论 speed 多少都**不**再变速。"""
        p = self._prepare()
        before = _wav_seconds(p)
        actual = apo.postprocess_wav(p, speed=2.0, max_pause_seconds=0.0, native_speed=True)
        self.assertAlmostEqual(actual, before, delta=0.02)


@unittest.skipUnless(_ffmpeg_available(), "需要 ffmpeg 才能实测静音压缩")
class PostprocessSilenceCompressTests(unittest.TestCase):
    """max_pause=0 不压；max_pause=0.2 能把 3 秒长静音压到 ~0.2 秒。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo_silence_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_max_pause_zero_does_not_change_silence(self):
        p = self.tmp / "ts.wav"
        _write_tone_silence_tone(p, tone_seconds=1.0, silence_seconds=3.0)
        before = _wav_seconds(p)   # 约 5s
        actual = apo.postprocess_wav(p, speed=1.0, max_pause_seconds=0.0, native_speed=False)
        self.assertAlmostEqual(actual, before, delta=0.02)

    def test_max_pause_compresses_long_silence(self):
        p = self.tmp / "ts.wav"
        _write_tone_silence_tone(p, tone_seconds=1.0, silence_seconds=3.0)
        # 1s + 3s 静音 + 1s → 5s；压到 max_pause=0.2 → 1 + 0.2 + 1 = 2.2s
        actual = apo.postprocess_wav(p, speed=1.0, max_pause_seconds=0.2, native_speed=False)
        self.assertAlmostEqual(actual, 2.2, delta=0.4)


class PostprocessFailureIsolationTests(unittest.TestCase):
    """处理失败保留旧 output，清临时——模拟 ffmpeg 失败，不依赖 ffmpeg。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo_fail_"))
        self.out = self.tmp / "old.wav"
        _write_tone(self.out, seconds=1.0)
        self._original = self.out.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ffmpeg_error_keeps_old_output(self):
        """模拟 atempo 阶段 ffmpeg 抛错——旧 output 原字节不动、无临时 .part.wav 残留。"""
        def _boom(cmd, what):
            raise apo.PostProcessError("simulated ffmpeg fail")

        with mock.patch.object(apo, "_resolve_ffmpeg", return_value="ffmpeg"), \
             mock.patch.object(apo, "_run_ffmpeg", side_effect=_boom):
            with self.assertRaises(apo.PostProcessError):
                apo.postprocess_wav(self.out, speed=2.0, max_pause_seconds=0.0,
                                     native_speed=False)
        # 旧 output 原样保留
        self.assertEqual(self.out.read_bytes(), self._original)
        # 不留 .part.wav / .step_*.part.wav
        for p in self.tmp.iterdir():
            self.assertFalse(p.name.endswith(".part.wav"), p)


class LongformPostprocessDispatchTests(unittest.TestCase):
    """longform 层根据引擎 EngineCapabilities.native_speed 决定是否调 atempo。
    mock 引擎会真走后处理；Edge（native_speed=True）时 atempo 步骤被跳过。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apo_long_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mock_engine_receives_speed_via_postprocess(self):
        """mock 是 native_speed=False；synthesize_long(per_line) 每段合成后调
        `_apply_postprocess`，间接调用 `apo.postprocess_wav`。
        通过 patch `longform.postprocess_wav`（`_apply_postprocess` 内部的 lazy import
        绑定点）验证：每段都调用了、每次 speed 值忠实传入、native_speed=False。"""
        # 不设 durations → 每行默认 5s，mock.synthesize_full 不会因行数不匹配报错
        eng = mock_engine.MockEngine()
        opts = SynthesisOptions(speed=2.0)
        calls: list[tuple] = []

        def _spy(path, *, speed, max_pause_seconds, native_speed):
            calls.append((Path(path).name, speed, max_pause_seconds, native_speed))
            # 不真跑 ffmpeg，只把源文件当作已处理返回其实际秒数
            return apo._wav_seconds_of(path)

        # 关键 patch 目标：`_apply_postprocess` 内 lazy import 后引用的是
        # dub_align_studio.engines.audio_postprocess.postprocess_wav —— patch apo 即可
        with mock.patch.object(apo, "postprocess_wav", side_effect=_spy):
            longform.synthesize_long(eng, "第一行\n第二行", None,
                                       self.tmp / "master.wav", opts,
                                       max_chars=1000, per_line=True)
        # 两段各调用一次 postprocess_wav；speed=2.0；native_speed=False
        self.assertGreaterEqual(len(calls), 2, f"calls={calls}")
        for _name, sp, _mp, native in calls:
            self.assertEqual(sp, 2.0)
            self.assertFalse(native)

    def test_native_speed_engine_prevents_double_speedup(self):
        """伪造 native_speed=True 的引擎（覆盖 capabilities 实例属性），
        验证后处理层收到 native_speed=True——防 Edge TTS 被二次变速。"""
        eng = mock_engine.MockEngine()
        eng.capabilities = EngineCapabilities(native_speed=True, detail="fake native")
        opts = SynthesisOptions(speed=2.0)

        called_with_native: list[bool] = []

        def _spy(path, *, speed, max_pause_seconds, native_speed):
            called_with_native.append(native_speed)
            return apo._wav_seconds_of(path)

        with mock.patch.object(apo, "postprocess_wav", side_effect=_spy):
            longform.synthesize_long(eng, "只有一行", None, self.tmp / "m.wav", opts,
                                       max_chars=1000, per_line=True)
        # 至少调用一次，且每次的 native_speed 都是 True（防被引擎侧变速后再二次变速）
        self.assertTrue(called_with_native, "postprocess_wav should have been invoked")
        self.assertTrue(all(called_with_native),
                         f"native_speed 期望全 True，实际 {called_with_native}")


class TransactionalCommitTests(unittest.TestCase):
    """P0-1 / P0-2 契约：

    · 后处理失败必须让任务失败（抛异常），不吞异常继续；
    · 引擎开始合成之前就存在的旧正式 master/chunk 在失败后仍必须**原字节保留**——
      不允许被引擎中间产物覆盖。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lf_txn_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_old_master(self, master: Path) -> bytes:
        """先手动写一份"旧成功产物"（master.wav + master_chunks/），返回旧 master 字节。"""
        _write_tone(master, seconds=1.0)
        chunks_dir = master.parent / f"{master.stem}_chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        _write_tone(chunks_dir / "chunk_001.wav", seconds=1.0)
        (chunks_dir / "分段清单.json").write_text(
            '{"master": "old", "chunks": [{"index":1,"line":1,"text":"旧","file":"chunk_001.wav","seconds":1.0}]}',
            encoding="utf-8")
        return master.read_bytes()

    def test_postprocess_failure_preserves_old_master_multi_chunk(self):
        """多段路径：某段后处理失败时旧 master 字节不动、旧 chunks 目录不动。"""
        from dub_align_studio.engines import longform, mock_engine
        from dub_align_studio.engines.base import SynthesisOptions
        master = self.tmp / "master.wav"
        old_bytes = self._seed_old_master(master)
        old_chunks_dir = master.parent / f"{master.stem}_chunks"
        old_chunk_bytes = (old_chunks_dir / "chunk_001.wav").read_bytes()

        eng = mock_engine.MockEngine()
        # opts 里 max_pause_seconds=0.2 → 会真调后处理；用 mock 让它抛
        opts = SynthesisOptions(speed=1.0, max_pause_seconds=0.2)

        def _boom(*a, **kw):
            raise apo.PostProcessError("模拟后处理失败")

        with mock.patch.object(apo, "postprocess_wav", side_effect=_boom):
            with self.assertRaises(longform.PostprocessRequired):
                longform.synthesize_long(eng, "第一行\n第二行\n第三行", None, master, opts,
                                          max_chars=1000, per_line=True)
        # 旧 master 与旧 chunk 原字节保留（引擎从不直接写正式 master）
        self.assertEqual(master.read_bytes(), old_bytes)
        self.assertEqual((old_chunks_dir / "chunk_001.wav").read_bytes(), old_chunk_bytes)
        # 无残留 staging 目录
        stagings = list(self.tmp.glob("master_chunks.staging_*"))
        self.assertEqual(stagings, [], f"残留 staging：{stagings}")

    def test_postprocess_failure_preserves_old_master_single_chunk(self):
        """单段路径同样：候选文件失败清干净，旧 master 不动。"""
        from dub_align_studio.engines import longform, mock_engine
        from dub_align_studio.engines.base import SynthesisOptions
        master = self.tmp / "solo.wav"
        _write_tone(master, seconds=1.0)
        old_bytes = master.read_bytes()

        eng = mock_engine.MockEngine()
        opts = SynthesisOptions(max_pause_seconds=0.2)

        def _boom(*a, **kw):
            raise apo.PostProcessError("模拟后处理失败")

        with mock.patch.object(apo, "postprocess_wav", side_effect=_boom):
            with self.assertRaises(longform.PostprocessRequired):
                longform.synthesize_long(eng, "只有一句。", None, master, opts,
                                          max_chars=1_000_000)
        self.assertEqual(master.read_bytes(), old_bytes)
        # 无残留 .staging.wav
        stagings = list(master.parent.glob("solo.wav.staging*"))
        self.assertEqual(stagings, [], f"残留：{stagings}")

    def test_redub_failure_preserves_old_chunk_and_master(self):
        """redub_chunk 后处理失败：旧 chunk 与旧 master 均原字节保留。"""
        from dub_align_studio.engines import longform, mock_engine
        from dub_align_studio.engines.base import SynthesisOptions
        master = self.tmp / "master.wav"
        # 先跑一遍成功的合成把 chunks + master + manifest 全部落地
        eng = mock_engine.MockEngine()
        longform.synthesize_long(eng, "\n".join(["甲。", "乙。", "丙。"]), None, master,
                                  SynthesisOptions(), max_chars=1_000_000, per_line=True)
        chunks_dir = master.parent / f"{master.stem}_chunks"
        old_master_bytes = master.read_bytes()
        old_chunk2_bytes = (chunks_dir / "chunk_002.wav").read_bytes()

        opts = SynthesisOptions(max_pause_seconds=0.2)

        def _boom(*a, **kw):
            raise apo.PostProcessError("模拟后处理失败")

        with mock.patch.object(apo, "postprocess_wav", side_effect=_boom):
            with self.assertRaises(longform.PostprocessRequired):
                longform.redub_chunk(eng, master, 2, None, opts)
        self.assertEqual(master.read_bytes(), old_master_bytes)
        self.assertEqual((chunks_dir / "chunk_002.wav").read_bytes(), old_chunk2_bytes)
        # 无残留 .staging.wav
        residues = list(chunks_dir.glob("*.staging.wav"))
        self.assertEqual(residues, [], residues)


if __name__ == "__main__":
    unittest.main()
