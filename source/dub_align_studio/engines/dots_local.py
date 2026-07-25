"""dots.tts 进程内适配：整篇文案 → 连贯 master.wav。

上游事实（https://github.com/rednote-hilab/dots.tts + PyPI dots.tts 0.2.1，2026-07-23 核实）：
    真实 Python API（子模块，非顶层）：
        from dots_tts.runtime import DotsTtsRuntime
        rt = DotsTtsRuntime.from_pretrained("rednote-hilab/dots.tts-soar",
                                            precision="bfloat16", optimize=True)
        result = rt.generate(text=..., prompt_audio_path=..., prompt_text=...,
                             num_steps=10, guidance_scale=1.2)
        # result 为 dict：{"audio": torch.Tensor, "sample_rate": int(48000)}
        soundfile.write(out, result["audio"].float().cpu().squeeze().numpy(),
                        result["sample_rate"])
    2B 全连续 AR，48kHz；零样本克隆（参考音频 + 可选转写）；需 NVIDIA GPU ≥6GB。

纪律：
    - 真引擎不进单元测试（非确定/需 GPU），只进 capability-check 探测（probe）；
      但「调用签名正确性」用注入的假 dots_tts.runtime 模块单测（不碰 GPU）。
    - 惰性导入：未装 dots.tts / 无 GPU 的机器上 import 本模块零副作用。
    - 运行时按 (检查点,精度,优化) 缓存，避免每次成片/试听重复加载 2B 模型。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path

from .base import (
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    wav_seconds,
    write_master_metadata,
)
from .voice_ref import VoiceRef
from .. import settings as studio_settings


# 检查点仓库（HuggingFace）：SOAR = 自纠偏对齐版，克隆相似度最好，默认选它。
DEFAULT_CHECKPOINT = "rednote-hilab/dots.tts-soar"
_PACKAGE = "dots_tts"            # pip 名 dots.tts → 导入名 dots_tts
_RUNTIME_MODULE = "dots_tts.runtime"   # DotsTtsRuntime 在此子模块
EXPECTED_SAMPLE_RATE = 48000
LOCAL_CHECKPOINT_DIR = "dots.tts-soar"

# 起音丢字兜底 · 牺牲音节方案（2026-07-24 实测定稿）：
# 纯标点引子（"，"/"。"）零音素、模型不为其生成音频帧，吸收不了任何东西——实测正文首字
# 仍被 AR 起始不稳掐掉（音频第 0ms 即满音量、无起音爬坡）。必须用**真发音的音节**当炮灰：
# 先发一声「嗯」+ 句停，被吃的落在「嗯」上；落盘时定位嗯后的停顿缺口，把「嗯+停顿」整体
# 切掉，正文以自己完整的自然起音开头。留空则关闭整套兜底。
_ONSET_LEAD_IN = "嗯。"

_SILENCE_THRESHOLD = 0.015     # 归一化幅度阈值（相对峰值）：低于视为静音
_LEADING_KEEP_MS = 20          # 兜底裁静音时起音前保留的余量
_MAX_LEADING_TRIM_S = 0.8      # 兜底裁静音的封顶
# 牺牲音节判定（_onset_cut_index）：把第一段发声当「嗯」切掉，切到其后第二段发声（正文）起点。
#   · _FILLER_MAX_S：第二段发声（正文首字）须在此之前出现，否则回退（不切正文）；
#   · _FILLER_VOICED_MAX_S：第一段发声 ≤ 此值才当「嗯」——「嗯」很短，真实首句普遍更长
#     （实测用户音频首句 0.79s > 0.6s，不会被误当填充音切掉）。
# 2026-07-25：与停顿多长无关——旧方案要求「嗯」后停顿 ≥50ms 才切，模型实际常只停几十毫秒
# → 门限不达标、「嗯」大量泄漏。现在只要「嗯」与正文之间有任何停顿边界（≥1 个整静音窗）就切。
_FILLER_GAP_MIN_S = 0.05       # 保留常量（不再作切割门限，_voiced_runs 的整窗断段即边界）
_FILLER_MAX_S = 1.0
_FILLER_VOICED_MAX_S = 0.6
_FILLER_KEEP_MS = 40           # 切到正文起点前回退这点余量，保住正文首音上升沿

# 尾部爆音净化（2026-07-24 实测：正文结束后隔 90ms 冒出 ~50ms、峰值 0.5 的孤立噪声脉冲，
# 是 AR 模型生成收尾的垃圾音）：从后往前，凡「与前面发声隔 ≥_TAIL_GAP_MIN_S 静音、
# 自身 ≤_TAIL_BURST_MAX_S 的孤立短发声」判为爆音删除（可连删多个）；真正文尾之后留
# _TAIL_KEEP_S 余量截断。真实的短尾字与正文间隔通常 <60ms，不会被误删。
_TAIL_BURST_MAX_S = 0.15
_TAIL_GAP_MIN_S = 0.06
_TAIL_KEEP_S = 0.15
# 切口防爆响：起音切点加淡入、尾部切点加淡出（毫秒）
_FADE_IN_MS = 8
_FADE_OUT_MS = 15

# 运行时缓存：{(checkpoint, precision, optimize): runtime}，跨多次合成复用已加载的大模型。
_RUNTIME_CACHE: dict = {}


def _component_python_root() -> Path:
    return studio_settings.components_root() / "dots.tts" / "python"


def _torch_python_root() -> Path:
    return studio_settings.components_root() / "torch" / "python"


def _ensure_component_python_path() -> None:
    for root in reversed([_torch_python_root(), _component_python_root()]):
        if root.exists() and str(root) not in sys.path:
            sys.path.insert(0, str(root))


def _installed() -> bool:
    _ensure_component_python_path()
    try:
        return importlib.util.find_spec(_PACKAGE) is not None
    except (ImportError, ValueError):
        return False


_TN_STUB_ACTIVE = False


def _ensure_tn_stub() -> bool:
    """dots.tts 的 dots_tts.utils.text 在导入时硬性 `from tn.chinese.normalizer import Normalizer`。

    `tn` 来自 WeTextProcessing（中文文本正则化），依赖 pynini —— Windows 编译不了，我们
    一贯跳过。真 `tn` 不在时，注入一个「原样返回」的桩模块，让 dots.tts 能正常导入并出声：
    只跳过「数字/符号口语化」这一步预处理，不影响零样本克隆音色与逐行对齐。
    返回 True 表示启用了桩（即真 tn 缺失）。真 tn 存在则不动，用它。
    """
    global _TN_STUB_ACTIVE
    if "tn" in sys.modules:
        return _TN_STUB_ACTIVE
    try:
        if importlib.util.find_spec("tn") is not None:
            return False  # 真 WeTextProcessing 在，优先用它
    except (ImportError, ValueError):
        pass
    import types

    class _PassthroughNormalizer:
        def __init__(self, *args, **kwargs):
            pass

        def normalize(self, text, *args, **kwargs):
            return text

    modules: dict = {}
    tn = types.ModuleType("tn")
    modules["tn"] = tn
    for lang in ("chinese", "english"):
        pkg = types.ModuleType(f"tn.{lang}")
        norm = types.ModuleType(f"tn.{lang}.normalizer")
        norm.Normalizer = _PassthroughNormalizer
        pkg.normalizer = norm
        setattr(tn, lang, pkg)
        modules[f"tn.{lang}"] = pkg
        modules[f"tn.{lang}.normalizer"] = norm
    sys.modules.update(modules)
    _TN_STUB_ACTIVE = True
    return True


def _local_checkpoint() -> Path | None:
    candidate = studio_settings.components_root() / "dots.tts" / LOCAL_CHECKPOINT_DIR
    required = ("model.safetensors", "vocoder.safetensors", "config.json")
    if candidate.is_dir() and all((candidate / name).exists() for name in required):
        return candidate
    return None


def _checkpoint_ref(checkpoint: str | None) -> str:
    if checkpoint:
        return checkpoint
    local = _local_checkpoint()
    return str(local) if local is not None else DEFAULT_CHECKPOINT


def _cuda_detail() -> tuple[bool, str]:
    try:
        torch = importlib.import_module("torch")
    except ImportError:
        return False, "未安装 torch。"
    if not torch.cuda.is_available():
        return False, "torch 已装但未检测到可用 CUDA GPU。"
    name = torch.cuda.get_device_name(0)
    free, total = torch.cuda.mem_get_info()
    return True, f"GPU：{name}，显存 {free / 2**30:.1f}/{total / 2**30:.1f} GB 可用（需 ≥6GB，推荐 ≥11GB）。"


_TORCH_CUDA_HINT = (
    "需要 CUDA 版 torch：推荐工具箱一键装，或手动 "
    "pip install --force-reinstall torch==2.11.0 torchaudio==2.11.0 "
    "--index-url https://download.pytorch.org/whl/cu126（勿用 cu121 源，已停更会装到旧版）。"
    "无 NVIDIA 显卡的电脑无法用 dots.tts，请改用 fish-speech 或 mock（测试）引擎。"
)


@dataclass
class DotsLocalEngine:
    """dots.tts 本地引擎。checkpoint 可换（base/soar/mf）。"""

    checkpoint: str | None = None
    seed: int = 42
    precision: str = "bfloat16"
    optimize: bool = False   # torch.compile 加速：首次编译慢且在部分 Windows 环境易失败，默认关
    # 单次合成字数上限：超长整篇会被 AR 模型截断/劣化 → 由 longform 分块拼接（保守取 120 中文字≈30~40s/段）
    max_chars: int = 120

    key: str = "dots_local"

    def probe(self) -> EngineStatus:
        if not _installed():
            return EngineStatus(
                key=self.key,
                available=False,
                detail="未安装 dots.tts（工具箱点「安装」，已内置国内镜像）；权重按需下载，不进安装包。",
            )
        _ensure_tn_stub()
        try:
            importlib.import_module(_RUNTIME_MODULE)
        except Exception as exc:
            return EngineStatus(
                key=self.key,
                available=False,
                detail=f"dots.tts 已找到但导入失败：{exc}{_import_failure_hint(exc)}",
            )
        cuda_ok, cuda_detail = _cuda_detail()
        if not cuda_ok:
            return EngineStatus(key=self.key, available=False,
                                detail=f"dots.tts 已装；{cuda_detail} {_TORCH_CUDA_HINT}")
        norm_note = ("；文本正则化用桩跳过（缺 WeTextProcessing/pynini，不影响出声，"
                     "建议文案里数字写成中文）" if _TN_STUB_ACTIVE else "")
        return EngineStatus(
            key=self.key,
            available=True,
            detail=f"dots.tts 可用（检查点 {_checkpoint_ref(self.checkpoint)}）；{cuda_detail}{norm_note}",
        )

    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        status = self.probe()
        if not status.available:
            raise EngineUnavailable(status.detail)
        if not text.strip():
            raise ValueError("整篇文案为空。")
        options = options or SynthesisOptions(seed=self.seed)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        self._generate(text, voice, output, options)

        seconds = wav_seconds(output)
        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice.voice_id if voice else "",
            model=_checkpoint_ref(self.checkpoint),
            seed=options.seed,
            sample_rate=EXPECTED_SAMPLE_RATE,
            seconds=round(seconds, 3),
            options=options.to_payload(),
        )
        write_master_metadata(master)
        return master

    # ------------------------------------------------------------------
    def _load_runtime(self):
        """加载（并缓存）DotsTtsRuntime。API：from dots_tts.runtime import DotsTtsRuntime。"""
        checkpoint = _checkpoint_ref(self.checkpoint)
        cache_key = (checkpoint, self.precision, self.optimize)
        cached = _RUNTIME_CACHE.get(cache_key)
        if cached is not None:
            return cached
        _ensure_tn_stub()
        try:
            runtime_mod = importlib.import_module(_RUNTIME_MODULE)
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", "") or str(exc)
            if missing.split(".")[0] not in ("dots_tts",):
                # dots.tts 本体在，但缺它的依赖（如 loguru/torch）——多为安装被中途停止/超时
                raise EngineUnavailable(
                    f"dots.tts 依赖未装全（缺 {missing}）。请到工具箱重新点 dots.tts「安装」并耐心等它完整跑完"
                    "（勿中途停止）；若缺 torch/CUDA，请先装「PyTorch GPU 版」。"
                ) from exc
            raise EngineUnavailable(
                f"无法导入 {_RUNTIME_MODULE}（{exc}）。请到工具箱重新安装 dots.tts。"
            ) from exc
        except ImportError as exc:
            raise EngineUnavailable(
                f"导入 {_RUNTIME_MODULE} 失败（{exc}）。请到工具箱重新安装 dots.tts。"
            ) from exc
        runtime_cls = getattr(runtime_mod, "DotsTtsRuntime", None)
        if runtime_cls is None:
            raise EngineUnavailable(
                f"{_RUNTIME_MODULE} 中未找到 DotsTtsRuntime。请核对 dots.tts 版本（本适配器按 0.2.x API）。"
            )
        runtime = runtime_cls.from_pretrained(
            checkpoint, precision=self.precision, optimize=self.optimize)
        _RUNTIME_CACHE[cache_key] = runtime
        return runtime

    def _generate(self, text: str, voice: VoiceRef | None, output: Path, options: SynthesisOptions) -> None:
        """按 dots.tts 0.2.x 真实 API 整篇生成并落盘。

        generate() 接受 text / prompt_audio_path / prompt_text / num_steps /
        guidance_scale 等参数，返回 {"audio": tensor, "sample_rate": int}。
        只传上游支持的参数（speed/max_pause 等非其入参，避免 TypeError）。
        """
        runtime = self._load_runtime()
        kwargs: dict = {
            "text": (_ONSET_LEAD_IN + text) if _ONSET_LEAD_IN else text,
            "num_steps": int(options.num_steps),
            "guidance_scale": float(options.guidance_scale),
            # 当前 dots.tts 0.2.1 runtime.generate 不接收 seed。
            # 先放入候选参数，再按真实签名过滤；若上游后续支持 seed，会自动传入。
            "seed": int(options.seed),
        }
        if options.normalize_text:
            kwargs["normalize_text"] = True
        if voice is not None:
            kwargs["prompt_audio_path"] = Path(voice.reference_wav).as_posix()
            if voice.transcript.strip():
                kwargs["prompt_text"] = voice.transcript.strip()  # 带转写：克隆相似度最高
        try:
            result = runtime.generate(**_supported_generate_kwargs(runtime, kwargs))
        except TypeError as exc:
            raise EngineUnavailable(
                f"dots.tts generate() 参数不匹配（{exc}）。请核对 dots.tts 版本或重新安装 dots.tts 组件。"
            ) from exc
        except Exception as exc:
            raise EngineUnavailable(f"dots.tts 整篇合成失败：{exc}") from exc
        self._save_result(result, output)

    @staticmethod
    def _save_result(result, output: Path) -> None:
        """把 generate 的返回落成 48kHz WAV。兼容 dict / 张量 / 文件路径三种返回形态。"""
        # ① 直接返回文件路径
        if isinstance(result, (str, Path)):
            src = Path(result)
            if src != output:
                output.write_bytes(src.read_bytes())
            return
        # ② 标准形态：{"audio": tensor, "sample_rate": int}
        audio, sample_rate = result, EXPECTED_SAMPLE_RATE
        if isinstance(result, dict):
            audio = result.get("audio")
            sample_rate = int(result.get("sample_rate", EXPECTED_SAMPLE_RATE))
        if audio is None:
            raise EngineUnavailable("dots.tts 返回结果为空（无 audio）。请核对 dots.tts 版本。")
        try:
            soundfile = importlib.import_module("soundfile")
        except ImportError as exc:
            raise EngineUnavailable("缺少 soundfile（pip install soundfile）用于落盘 dots.tts 音频。") from exc
        array = _trim_leading_silence(_to_numpy(audio), sample_rate)
        soundfile.write(str(output), array, sample_rate)


def _transformers_diag() -> str:
    """报出当前进程实际加载的 transformers 版本与路径，用于判断是否装到了另一个 Python。"""
    try:
        tf = importlib.import_module("transformers")
    except Exception as exc:  # noqa: BLE001 — transformers 本身都导不进来
        return f"transformers 无法导入（{exc}）"
    ver = getattr(tf, "__version__", "?")
    where = getattr(tf, "__file__", "?")
    return f"transformers {ver}（{where}）"


def _import_failure_hint(exc: Exception) -> str:
    """dots.tts 导入失败时，把「装到了哪个环境」直接摆到用户面前——
    多数不是没装，而是 pip 装进了另一个 Python，或跑的是看不到系统包的打包版。"""
    text = str(exc)
    if "Qwen2" not in text and "transformers" not in text.lower():
        return ""
    frozen = ""
    if getattr(sys, "frozen", False):
        frozen = ("；注意：本软件是打包版(frozen)，启动时会自动桥接本机同版本系统 Python 的依赖 —— "
                  "请确认本机装有 Python 3.11 且其中已装好 dots.tts 全套（工具箱点「安装」即可装进去），"
                  "或改用「整合离线版」")
    return (
        f"。诊断：{_transformers_diag()}，运行环境 {sys.executable}{frozen}。"
        "dots.tts 需要 transformers==4.57.0（5.x 过新同样缺 Qwen2）。"
        "若你已 pip 安装 4.57.0 但这里仍显示别的版本/路径，说明装到了另一个 Python —— "
        "请用「运行本软件的同一个 Python」重装，或到工具箱点 dots.tts「安装」。命令："
        "pip install \"transformers==4.57.0\" \"accelerate==1.12.0\" "
        "-i https://pypi.tuna.tsinghua.edu.cn/simple。"
    )


def _leading_trim_index(abs_samples, sample_rate: int, peak: float) -> int:
    """纯逻辑：给定单声道 |样本| 序列，返回应从第几个样本开始播放（裁掉开头静音）。

    - 阈值 = 相对峰值的 _SILENCE_THRESHOLD；
    - 只在前 _MAX_LEADING_TRIM_S 内找首个过阈样本（封顶，避免误伤）；
    - 找到后回退 _LEADING_KEEP_MS 留出上升沿；前 cap 内全静音则不裁（返回 0）。
    可脱离 numpy 单测。
    """
    thr = _SILENCE_THRESHOLD * (peak or 1.0)
    cap = int(_MAX_LEADING_TRIM_S * sample_rate)
    limit = min(len(abs_samples), cap)
    first = -1
    for i in range(limit):
        if abs_samples[i] > thr:
            first = i
            break
    if first < 0:
        return 0
    keep = int(_LEADING_KEEP_MS / 1000.0 * sample_rate)
    return max(0, first - keep)


def _onset_cut_index(abs_samples, sample_rate: int, peak: float) -> int:
    """纯逻辑：牺牲音节「嗯」切点。返回应从第几个样本开始播放。

    2026-07-25 重写（用户实测「嗯」仍大量泄漏）：**与停顿多长无关**。
    我们总在正文前注入「嗯。」→ 它必是第一段发声。策略：
      ① 定位第一段发声（10ms 窗，任何一个整静音窗即断段）；
      ② 若它靠开头（≤_MAX_LEADING_TRIM_S）且够短（≤_FILLER_VOICED_MAX_S，确是「嗯」而非正文），
         且其后还有第二段发声（=正文首字）在 _FILLER_MAX_S 内 → 切到正文起点前 _FILLER_KEEP_MS；
      ③ 否则（「嗯」与正文黏连成一长段 / 其后无正文）→ 回退只裁开头静音，绝不切正文。
    旧方案要求「嗯」后停顿 ≥_FILLER_GAP_MIN_S(50ms) 才切，而模型实际常只停几十毫秒
    → 门限不达标、全部走兜底、「嗯」被保留。现在只要「嗯」与正文之间有**任何**停顿边界就切。
    """
    win = max(1, int(sample_rate * 0.010))
    thr = _SILENCE_THRESHOLD * (peak or 1.0)
    scan = abs_samples[: int((_FILLER_MAX_S + 1.0) * sample_rate)]
    runs = _voiced_runs(scan, win, thr)
    if not runs:
        return 0  # 全静音，不动
    r0_start, r0_end = runs[0]
    early = int(_MAX_LEADING_TRIM_S / 0.010)      # 「嗯」必须靠开头
    short = int(_FILLER_VOICED_MAX_S / 0.010)     # 「嗯」很短；正文首句普遍更长
    deadline = int(_FILLER_MAX_S / 0.010)
    if r0_start <= early and (r0_end - r0_start) <= short and len(runs) >= 2:
        body_start = runs[1][0]                    # 第二段发声 = 正文首字
        if body_start <= deadline:
            return max(0, body_start * win - int(_FILLER_KEEP_MS / 1000.0 * sample_rate))
    return _leading_trim_index(abs_samples, sample_rate, peak)  # 无第二段/首段过长 → 只裁开头静音


def _voiced_runs(abs_samples, win: int, thr: float) -> list[tuple[int, int]]:
    """10ms 窗发声段：返回 [(起窗, 止窗排他), …]。纯逻辑。"""
    n = len(abs_samples) // win
    runs: list[tuple[int, int]] = []
    start = -1
    for i in range(n):
        seg = abs_samples[i * win:(i + 1) * win]
        voiced = bool(len(seg)) and max(seg) > thr
        if voiced and start < 0:
            start = i
        elif not voiced and start >= 0:
            runs.append((start, i))
            start = -1
    if start >= 0:
        runs.append((start, n))
    return runs


def _tail_cut_index(abs_samples, sample_rate: int, peak: float) -> int:
    """纯逻辑：尾部爆音净化后的截止样本。孤立短爆音（隔≥gap、长≤burst）从后连删；
    真正文尾后留 _TAIL_KEEP_S。无发声/无可删时只按末段收尾。"""
    win = max(1, int(sample_rate * 0.010))
    thr = _SILENCE_THRESHOLD * (peak or 1.0)
    runs = _voiced_runs(abs_samples, win, thr)
    if not runs:
        return len(abs_samples)
    burst_w = int(_TAIL_BURST_MAX_S / 0.010)
    gap_w = max(1, int(_TAIL_GAP_MIN_S / 0.010))
    dropped_start = None
    while len(runs) > 1:
        s, e = runs[-1]
        prev_end = runs[-2][1]
        if (e - s) <= burst_w and (s - prev_end) >= gap_w:
            dropped_start = s   # 记录最早被删爆音的起点（从后往前删，最后记到的最早）
            runs.pop()
            continue
        break
    end = runs[-1][1] * win + int(_TAIL_KEEP_S * sample_rate)
    if dropped_start is not None:
        end = min(end, dropped_start * win)  # 余量不得越过被删爆音——否则等于没删
    return min(len(abs_samples), end)


def _trim_leading_silence(array, sample_rate: int):
    """段音频两端净化：裁「牺牲音节+停顿」/开头静音 + 删尾部孤立爆音并截尾，
    切口加淡入/淡出防爆响。numpy 缺失时原样返回。"""
    try:
        import numpy as np
    except Exception:
        return array
    a = np.asarray(array)
    if a.ndim == 0 or a.size == 0:
        return array
    if a.ndim == 1:
        mono = np.abs(a)
        time_axis = 0
    else:
        time_axis = int(np.argmax(a.shape))  # 时间轴取最长的一维
        other = tuple(ax for ax in range(a.ndim) if ax != time_axis)
        mono = np.abs(a).mean(axis=other)
    peak = float(np.max(mono)) if mono.size else 0.0
    start = _onset_cut_index(mono, sample_rate, peak) if _ONSET_LEAD_IN else _leading_trim_index(mono, sample_rate, peak)
    end = _tail_cut_index(mono, sample_rate, peak)
    if end <= start:
        start, end = 0, len(mono)
    if a.ndim == 1:
        a = a[start:end]
    else:
        a = a[start:end, ...] if time_axis == 0 else a[..., start:end]
    # 切口淡入/淡出：保证边界从零起落，拼接/截断处零爆响
    a = np.array(a, dtype=np.float32, copy=True)
    n_in = min(a.shape[time_axis] if a.ndim > 1 else len(a), int(_FADE_IN_MS / 1000.0 * sample_rate))
    n_out = min(a.shape[time_axis] if a.ndim > 1 else len(a), int(_FADE_OUT_MS / 1000.0 * sample_rate))
    if a.ndim == 1:
        if n_in > 1:
            a[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
        if n_out > 1:
            a[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return a


def _to_numpy(audio):
    """torch.Tensor → numpy（float, cpu, squeeze）；已是 numpy/list 则原样。"""
    for step in ("float", "cpu", "squeeze"):
        method = getattr(audio, step, None)
        if callable(method):
            audio = method()
    to_numpy = getattr(audio, "numpy", None)
    return to_numpy() if callable(to_numpy) else audio


def _supported_generate_kwargs(runtime, kwargs: dict) -> dict:
    """按 runtime.generate 的真实签名过滤参数，兼容 dots.tts 小版本差异。"""
    try:
        signature = inspect.signature(runtime.generate)
    except (TypeError, ValueError):
        safe = {"text", "prompt_audio_path", "prompt_text", "num_steps", "guidance_scale"}
        return {key: value for key, value in kwargs.items() if key in safe}
    params = signature.parameters
    accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())
    if accepts_kwargs:
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}
