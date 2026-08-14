"""R14 显卡能力档案 + 并发基准 + 自动化模式。

设计要点：

- 只依赖 FFmpeg **实际并发编码成功与吞吐**决定推荐并发。
  vendor 工具（nvidia-smi、intel_gpu_top、rocm-smi）只做补充信息，不作为
  推荐依据；有无也不影响最终能力判定。
- 基准素材必须是**用户代表性原视频**——320x240 synthetic sample 得到的
  结果与真实生产链路差距可达数量级，不能拿来做万级并发决策。
- 基准阶梯 [1,2,3,4,6,8,12,16]，任一档出现硬件失败 / 会话上限 / 显存不足
  即"提前停止"，不再放大。
- 推荐并发选择：从最大的成功档回退——聚合吞吐必须比"上一稳定档"提升
  ≥5%；否则退回更低档。绝不无条件默认 16。
- profile 通过 tmp+atomic replace 写入 `<data_root>/批量带货/gpu_profiles.json`；
  损坏文件在读取时安全忽略并按需触发重测。
- 设备指纹 = OS + FFmpeg 版本 + 编码器 + GPU/adapter + 驱动（能拿到就带；
  拿不到就写空串），指纹变化 → profile 失效必须重新基准。
- 生产池运行时禁止启动基准（会与真正的任务抢显卡/磁盘/CPU）；服务层负责
  在 pause_for_benchmark 前后同步 scheduler 状态。

这个模块**不修改 scheduler**；controller 由 concurrency_controller.py 组合
profile + 运行时状态来推动 scheduler.resize_pools。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from integrated_workbench.proc import popen_silent, run_silent

from .hw_encoder import (
    EncoderProbe, FAMILY_PREFERENCE, _CANDIDATES, null_sink, resolve_encoder,
    _list_encoders,
)


class BenchmarkProcessRegistry:
    """R14-FIX-3 P0-1：**每个 benchmark 独立**的子进程登记表。

    上一轮 `service.stop()` 遍历 `integrated_workbench.proc._ACTIVE`
    终止全部进程——这是跨组件误杀（生产队列、其他页面工具都会被杀）。
    修法：benchmark 自己 popen 的每个 FFmpeg 都注册到本 registry；
    cancel/stop 只 terminate/kill/wait 本 registry 里的进程，绝不碰全局。
    进程在 finally 中注销。
    """

    def __init__(self) -> None:
        self._procs: set = set()
        self._lock = threading.Lock()

    def register(self, proc) -> None:
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc) -> None:
        with self._lock:
            self._procs.discard(proc)

    def snapshot(self) -> list:
        with self._lock:
            return [p for p in self._procs if p.poll() is None]

    def terminate_all(self, grace_seconds: float = 3.0,
                       kill_wait: float = 2.0) -> int:
        """按 terminate → wait(grace) → kill → wait(kill_wait) 收尸。
        返回真正被 terminate 的进程数。"""
        procs = self.snapshot()
        for p in procs:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass
        for p in procs:
            try:
                p.wait(timeout=grace_seconds)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                    try:
                        p.wait(timeout=kill_wait)
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self._procs.clear()
        return len(procs)


def _popen_bench(cmd: list, registry: "BenchmarkProcessRegistry | None",
                   **kwargs):
    """popen_silent 包装：登记到 benchmark registry（若传入）。"""
    proc = popen_silent(cmd, **kwargs)
    if registry is not None:
        registry.register(proc)
    return proc


PROFILE_SCHEMA = "bulk_dub_gpu_profile@v1"
PROFILE_FILENAME = "gpu_profiles.json"
# R14-FIX P0-5：控制器持久化状态（mode/user_max/manual_video/profile_fingerprint）
STATE_SCHEMA = "bulk_dub_gpu_state@v1"
STATE_FILENAME = "gpu_state.json"

# 基准阶梯——不允许直接跳到 16
DEFAULT_LADDER: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 16)

# 推荐并发选择的吞吐提升阈值：低于此比例 → 优先选更低档
IMPROVEMENT_THRESHOLD = 0.05

# 硬件失败/会话上限 stderr 关键字（不区分大小写）
_HW_FAILURE_KEYS = (
    "session limit", "openencodesession", "out of memory",
    "device not available", "no cuda-capable device",
    "cannot load nvcuda", "nvenc", "qsv", "amf",
)


@dataclass
class LadderRunResult:
    concurrency: int
    successes: int
    failures: int
    hw_fallbacks: int
    session_limit_errors: int
    oom_errors: int
    device_errors: int
    wall_clock_seconds: float
    aggregate_fps: float
    realtime_multiple: float          # aggregate_fps / sample_fps
    projected_renders_per_hour: float
    projected_renders_per_day: float
    is_recommendable: bool
    # R14-FIX2 P1-2：**明确的硬件错误计数**——_classify_stderr("hw") 命中数。
    # 之前 hw_fallbacks 恒为 0，导致 hw_failures_total 恒为 0。
    hardware_errors: int = 0
    notes: str = ""


@dataclass
class DeviceCapability:
    """R14 设备能力档案——**指纹变化即失效**。"""
    schema: str = PROFILE_SCHEMA
    device_fingerprint: str = ""      # sha256(OS + ffmpeg_version + encoder + gpu + driver + encoder_preference)
    os_name: str = ""
    os_release: str = ""
    ffmpeg_path: str = ""
    ffmpeg_version: str = ""
    encoder_family: str = ""          # nvidia / intel / amd / cpu
    encoder_name: str = ""            # h264_nvenc / h264_qsv / h264_amf / libx264
    # R14-FIX2 P1-5：**保存测试时使用的 encoder_preference**——apply 时按
    # 相同 family/name 探测；否则显式 CPU/NVIDIA/Intel/AMD 的 profile 会被
    # 用"auto"重探到的结果错误比较。
    encoder_preference: str = "auto"
    gpu_adapter: str = ""             # 尽力取；拿不到写空
    driver_version: str = ""
    sample_video_path: str = ""
    sample_width: int = 0
    sample_height: int = 0
    sample_fps: float = 0.0
    sample_seconds: float = 0.0
    filter_chain_kind: str = "mirror_concat_130_v1"   # 与生产 build_filter_chain 对齐
    tested_at: float = 0.0
    tested_ladder: list[int] = field(default_factory=list)
    ladder_results: list[dict] = field(default_factory=list)   # LadderRunResult.asdict()
    max_successful_concurrency: int = 0
    recommended_concurrency: int = 0
    peak_renders_per_hour: float = 0.0
    peak_renders_per_day: float = 0.0
    reached_10k_per_day: bool = False
    reason: str = ""                  # 推荐并发的选择理由
    hw_failures_total: int = 0
    session_limit_errors_total: int = 0
    oom_errors_total: int = 0
    cpu_fallback_total: int = 0

    def to_json(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# 设备指纹与元数据采集
# --------------------------------------------------------------------------


def _ffmpeg_version(ffmpeg: str) -> str:
    try:
        r = run_silent([ffmpeg, "-hide_banner", "-version"],
                        capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=10)
    except Exception:  # noqa: BLE001
        return ""
    line = (r.stdout or "").splitlines()[:1]
    return (line[0].strip() if line else "")[:200]


def _try(cmd: list[str], timeout: float = 8.0) -> str:
    """静默尝试执行 vendor 工具，只取头部；失败返回空串。
    vendor 工具**永远只作补充**——拿不到不影响能力判定。"""
    try:
        r = run_silent(cmd, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()[:400]


def probe_gpu_metadata(family: str) -> tuple[str, str]:
    """尝试取 GPU 名称与驱动版本；拿不到写空串。**不作为能力判定依据**。

    返回 (adapter_name, driver_version)
    """
    adapter = ""
    driver = ""
    try:
        if family == "nvidia":
            raw = _try([
                "nvidia-smi", "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ])
            if raw:
                first = raw.splitlines()[0]
                parts = [p.strip() for p in first.split(",")]
                if len(parts) >= 1:
                    adapter = parts[0]
                if len(parts) >= 2:
                    driver = parts[1]
        elif family == "intel":
            # Linux 优先 lspci；Windows 上多半没工具，静默留空即可
            raw = _try(["lspci", "-nn"])
            for line in raw.splitlines():
                if "VGA" in line and "Intel" in line:
                    adapter = line.split(":", 2)[-1].strip()
                    break
        elif family == "amd":
            raw = _try(["rocm-smi", "--showproductname"])
            for line in raw.splitlines():
                if "Card series" in line or "Card model" in line:
                    adapter = line.split(":", 1)[-1].strip()
                    break
            if not adapter:
                raw = _try(["lspci", "-nn"])
                for line in raw.splitlines():
                    if "VGA" in line and ("AMD" in line or "ATI" in line):
                        adapter = line.split(":", 2)[-1].strip()
                        break
        elif family == "cpu":
            adapter = platform.processor() or ""
    except Exception:  # noqa: BLE001
        pass
    return adapter[:120], driver[:80]


def compute_fingerprint(*, os_name: str, ffmpeg_version: str,
                         encoder_name: str, gpu_adapter: str,
                         driver_version: str,
                         encoder_preference: str = "auto") -> str:
    """R14-FIX2 P1-5：encoder_preference 纳入指纹——显式 CPU 与 auto→CPU
    落在**不同**指纹上，避免旧显式 CPU profile 在有 GPU 的机上被误认可用。"""
    import hashlib
    material = "|".join([
        os_name or "", ffmpeg_version or "", encoder_name or "",
        gpu_adapter or "", driver_version or "",
        encoder_preference or "auto",
    ])
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:32]


def detect_capability_metadata(ffmpeg: str, encoder_preference: str = "auto",
                                *, sample_probe: Any | None = None,
                                sample_path: str | Path | None = None
                                ) -> DeviceCapability:
    """探测**当前**编码器/GPU/驱动，返回未做基准的 DeviceCapability 骨架。

    - 若 sample_probe / sample_path 传入 → 记录代表样本元数据；
    - 不做多档基准（那由 run_benchmark 独立执行）。
    """
    from .ffmpeg_pipeline import ffprobe_video  # 延迟导入以避免环
    probe = resolve_encoder(ffmpeg, preference=encoder_preference)
    family = probe.family
    adapter, driver = probe_gpu_metadata(family)
    fv = _ffmpeg_version(ffmpeg)
    os_name = platform.system() or ""
    os_release = platform.release() or ""
    fp = compute_fingerprint(
        os_name=os_name, ffmpeg_version=fv,
        encoder_name=probe.encoder, gpu_adapter=adapter,
        driver_version=driver,
        encoder_preference=encoder_preference or "auto",
    )
    cap = DeviceCapability(
        device_fingerprint=fp, os_name=os_name, os_release=os_release,
        ffmpeg_path=ffmpeg, ffmpeg_version=fv,
        encoder_family=family, encoder_name=probe.encoder,
        encoder_preference=encoder_preference or "auto",
        gpu_adapter=adapter, driver_version=driver,
    )
    if sample_path is not None:
        cap.sample_video_path = str(sample_path)
        if sample_probe is not None:
            cap.sample_width = int(getattr(sample_probe, "width", 0) or 0)
            cap.sample_height = int(getattr(sample_probe, "height", 0) or 0)
            cap.sample_fps = float(getattr(sample_probe, "fps", 0) or 0)
            cap.sample_seconds = float(getattr(sample_probe, "duration", 0) or 0)
        else:
            try:
                probe_data = ffprobe_video(sample_path)
                cap.sample_width = probe_data.width
                cap.sample_height = probe_data.height
                cap.sample_fps = probe_data.fps
                cap.sample_seconds = probe_data.duration
            except Exception:  # noqa: BLE001
                pass
    return cap


# --------------------------------------------------------------------------
# profile 持久化（原子写；损坏静默忽略）
# --------------------------------------------------------------------------


def _profile_dir() -> Path:
    """profile 文件目录：`<data_root>/批量带货/`。"""
    from .. import settings as studio_settings
    d = studio_settings.data_root() / "批量带货"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_path() -> Path:
    return _profile_dir() / PROFILE_FILENAME


def state_path() -> Path:
    return _profile_dir() / STATE_FILENAME


def save_state(state: dict) -> Path:
    """R14-FIX P0-5：原子写入 gpu_state.json；损坏/不匹配 schema 时静默忽略。"""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp.{uuid.uuid4().hex[:8]}"
    payload = dict(state)
    payload["schema"] = STATE_SCHEMA
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    try:
        fd = os.open(str(tmp), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass
    os.replace(str(tmp), str(p))
    return p


def load_state() -> dict | None:
    p = state_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return None
    return {k: v for k, v in data.items() if k != "schema"}


MAX_LADDER_STEPS = 16


def validate_ladder(raw: Iterable[int], *,
                     max_value: int | None = None) -> list[int]:
    """R14-FIX-3 P0-7：ladder 服务端硬限制——每档 ∈ [1, max_value]，
    去重递增，长度 ≤ MAX_LADDER_STEPS。非法直接抛 ValueError。
    """
    from .concurrency_controller import MAX_VIDEO_CONCURRENCY as _MVC
    cap = int(max_value if max_value is not None else _MVC)
    items: list[int] = []
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            raise ValueError(f"ladder 元素不是整数：{x!r}")
        if v < 1 or v > cap:
            raise ValueError(f"ladder 元素 {v} 超出范围 [1, {cap}]")
        items.append(v)
    if not items:
        raise ValueError("ladder 不能为空")
    if len(items) > MAX_LADDER_STEPS:
        raise ValueError(
            f"ladder 长度 {len(items)} 超上限 {MAX_LADDER_STEPS}"
        )
    dedup = sorted(set(items))
    return dedup


def _validate_and_repair_loaded_profile(
    data: dict,
) -> DeviceCapability | None:
    """R14-FIX-3 P1-4：加载 profile 时验证数据边界。
    - recommended_concurrency ∈ [1, MAX_VIDEO_CONCURRENCY]
    - encoder_preference ∈ 允许集合（或 'auto'）
    - ladder_results / tested_ladder 值合法
    非法 → 返回 None，走"重新基准"路径。
    """
    from .concurrency_controller import MAX_VIDEO_CONCURRENCY as _MVC
    from .hw_encoder import FAMILY_PREFERENCE as _FP
    try:
        cap = DeviceCapability(**{k: v for k, v in data.items()
                                    if k in DeviceCapability.__annotations__})
    except TypeError:
        return None
    if cap.recommended_concurrency:
        if not (1 <= int(cap.recommended_concurrency) <= _MVC):
            return None
    pref = cap.encoder_preference or "auto"
    if pref not in _FP and pref != "auto":
        return None
    try:
        if cap.tested_ladder:
            for v in cap.tested_ladder:
                if not (1 <= int(v) <= _MVC):
                    return None
        if cap.ladder_results:
            for r in cap.ladder_results:
                c = int((r or {}).get("concurrency") or 0)
                if c and not (1 <= c <= _MVC):
                    return None
    except (TypeError, ValueError):
        return None
    return cap


def load_profile(path: Path | None = None) -> DeviceCapability | None:
    """R14 读取 profile；文件不存在 / 损坏 / schema 不认得 → None（安全忽略）。
    R14-FIX-3 P1-4：加载后校验 recommended_concurrency / encoder_preference /
    ladder 值，越界的旧 profile 视为损坏。"""
    p = path or profile_path()
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != PROFILE_SCHEMA:
        return None
    return _validate_and_repair_loaded_profile(data)


def save_profile(cap: DeviceCapability, path: Path | None = None) -> Path:
    """R14 原子写入 profile：tmp 同目录 + os.replace；崩溃不留截断 JSON。
    父目录 fsync（best-effort）。"""
    p = path or profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp.{uuid.uuid4().hex[:8]}"
    data = cap.to_json()
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    # 尽力 fsync 文件与目录（不阻塞）
    try:
        fd = os.open(str(tmp), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    except OSError:
        pass
    os.replace(str(tmp), str(p))
    try:
        if os.name != "nt":
            fd = os.open(str(p.parent), os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
    except OSError:
        pass
    return p


# --------------------------------------------------------------------------
# 基准运行
# --------------------------------------------------------------------------


class BenchmarkCancelled(Exception):
    """基准被用户/系统取消。"""


def _classify_stderr(text: str) -> dict[str, int]:
    """判定 stderr 尾部是否命中硬件失败/会话上限/OOM/设备错误。"""
    t = (text or "").lower()
    out = {"hw": 0, "session_limit": 0, "oom": 0, "device": 0}
    if not t:
        return out
    if "out of memory" in t or "cudaerror" in t and "memory" in t:
        out["oom"] = 1
    if "session limit" in t or "openencodesession" in t:
        out["session_limit"] = 1
    if "no cuda-capable device" in t or "device not available" in t \
            or "no device available" in t:
        out["device"] = 1
    for k in _HW_FAILURE_KEYS:
        if k in t:
            out["hw"] = 1
            break
    return out


@dataclass
class _BenchmarkContext:
    ffmpeg: str
    input_video: Path
    tts_audio: Path
    staging_root: Path
    encoder: EncoderProbe
    zoom_percent: int
    keep_original_audio: bool
    preset: str
    crf: int
    filter_lines: list[str]
    map_args: list[str]
    sample_fps: float


def _prepare_bench_context(*, ffmpeg: str, input_video: Path,
                            tts_audio: Path, staging_root: Path,
                            encoder: EncoderProbe,
                            zoom_percent: int, keep_original_audio: bool,
                            preset: str, crf: int) -> _BenchmarkContext:
    """预备基准 context——**使用生产同款 build_filter_chain**。"""
    from .ffmpeg_pipeline import build_filter_chain, ffprobe_video, ffprobe_seconds
    probe = ffprobe_video(input_video)
    tts_seconds = ffprobe_seconds(tts_audio)
    if tts_seconds <= 0:
        raise ValueError("TTS 音频时长无效")
    final_seconds = min(probe.duration * 2, tts_seconds)
    filter_lines, map_args = build_filter_chain(
        width=probe.width, height=probe.height,
        zoom_percent=zoom_percent,
        keep_original_audio=keep_original_audio and probe.has_audio,
        final_seconds=final_seconds,
    )
    return _BenchmarkContext(
        ffmpeg=ffmpeg, input_video=input_video, tts_audio=tts_audio,
        staging_root=staging_root, encoder=encoder,
        zoom_percent=zoom_percent, keep_original_audio=keep_original_audio,
        preset=preset, crf=crf,
        filter_lines=filter_lines, map_args=map_args,
        sample_fps=probe.fps or 30.0,
    )


def _build_render_cmd(ctx: _BenchmarkContext, out_path: Path) -> list[str]:
    filter_complex = ";".join(ctx.filter_lines)
    cmd = [
        ctx.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(ctx.input_video),
        "-i", str(ctx.tts_audio),
        "-filter_complex", filter_complex,
        *ctx.map_args,
        "-c:v", ctx.encoder.encoder, *ctx.encoder.args,
        "-pix_fmt", "yuv420p",
    ]
    if ctx.encoder.encoder == "libx264":
        cmd += ["-preset", ctx.preset, "-crf", str(int(ctx.crf))]
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    return cmd


def _run_one(ctx: _BenchmarkContext, idx: int, cancel_event: threading.Event,
              slot_dir: Path, results: list[dict], results_lock: threading.Lock,
              per_job_timeout: float,
              expected_seconds: float | None = None,
              duration_tolerance: float = 0.4,
              registry: BenchmarkProcessRegistry | None = None) -> None:
    """单次编码 job。写受控 staging；结束后清 mp4。

    R14-FIX P1-1：**并发排空 stderr**——用后台线程 drain pipe，避免 ffmpeg
    因 stderr 写满 pipe buffer 阻塞导致假 hang；校验产物 returncode + size +
    视频流 + 音频流 + 时长容差，全部通过才算 ok。

    R14-FIX-3 P0-1 / P1-1：本次 benchmark 的所有 FFmpeg 进程都登记到本次
    `registry`（本方 owned），cancel/timeout 时**必须** terminate → wait →
    kill → wait 收尸并注销；不再依赖全局 `_ACTIVE`。
    """
    slot_dir.mkdir(parents=True, exist_ok=True)
    out_path = slot_dir / f"bench_{idx}.mp4"
    cmd = _build_render_cmd(ctx, out_path)
    started = time.time()
    proc = _popen_bench(cmd, registry, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace")
    stderr_buf: list[str] = []
    stderr_lock = threading.Lock()

    def _drain() -> None:
        try:
            for line in (proc.stderr or []):
                with stderr_lock:
                    stderr_buf.append(line)
                    if len(stderr_buf) > 120:
                        del stderr_buf[:-120]
        except Exception:  # noqa: BLE001
            pass

    drain_t = threading.Thread(target=_drain, name=f"bench-drain-{idx}",
                                 daemon=True)
    drain_t.start()

    def _terminate_and_wait(reason_stderr: str, rc: int) -> None:
        # P1-1：terminate → wait(3) → kill → wait(2)；不留僵尸
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        with results_lock:
            results.append({
                "ok": False, "returncode": rc,
                "seconds": time.time() - started,
                "stderr": reason_stderr,
                "size": 0, "duration_ok": False, "streams_ok": False,
            })

    try:
        while True:
            if cancel_event.is_set():
                _terminate_and_wait("cancelled", -1)
                return
            if (time.time() - started) > per_job_timeout:
                _terminate_and_wait(
                    f"timeout>{per_job_timeout:.0f}s", -2,
                )
                return
            code = proc.poll()
            if code is not None:
                break
            time.sleep(0.1)
    finally:
        drain_t.join(timeout=2)
        if proc.stderr:
            try: proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass
        if registry is not None:
            registry.unregister(proc)
    seconds = time.time() - started
    # R14-FIX P1-1 校验产物：returncode + size + 流 + 时长容差
    rc_ok = (proc.returncode == 0)
    size = out_path.stat().st_size if out_path.is_file() else 0
    size_ok = size >= 1024
    streams_ok = False
    duration_ok = False
    actual_seconds = 0.0
    if rc_ok and size_ok and out_path.is_file():
        try:
            from .ffmpeg_pipeline import has_video_and_audio_streams, ffprobe_seconds
            has_v, has_a = has_video_and_audio_streams(out_path)
            streams_ok = has_v and has_a
            actual_seconds = ffprobe_seconds(out_path)
            if expected_seconds is not None:
                duration_ok = abs(actual_seconds - expected_seconds) <= duration_tolerance
            else:
                duration_ok = actual_seconds > 0
        except Exception:  # noqa: BLE001
            streams_ok = False
            duration_ok = False
    ok = rc_ok and size_ok and streams_ok and duration_ok
    with stderr_lock:
        stderr_tail = "".join(stderr_buf).strip()[-500:]
    # 尽力清理 mp4；忽略失败
    try:
        if out_path.exists():
            out_path.unlink()
    except OSError:
        pass
    with results_lock:
        results.append({
            "ok": ok, "returncode": int(proc.returncode or 0),
            "seconds": seconds, "stderr": stderr_tail, "size": size,
            "duration_ok": duration_ok, "streams_ok": streams_ok,
            "actual_seconds": actual_seconds,
        })


def run_one_ladder_step(ctx: _BenchmarkContext, concurrency: int,
                          *, cancel_event: threading.Event,
                          per_job_timeout: float,
                          warmup: bool = False,
                          expected_seconds: float | None = None,
                          duration_tolerance: float = 0.4,
                          registry: BenchmarkProcessRegistry | None = None
                          ) -> LadderRunResult:
    """跑一档：同时启动 N 个 FFmpeg，等全部结束，聚合统计。
    R14-FIX P1-1：每个 job 都校验 returncode/size/流/时长；stderr 并发排空。
    R14-FIX-3 P0-1：本档所有进程登记到本 benchmark 的 `registry`。
    """
    if cancel_event.is_set():
        raise BenchmarkCancelled("benchmark cancelled before ladder step")
    step_root = ctx.staging_root / f"step_c{concurrency}_{uuid.uuid4().hex[:6]}"
    step_root.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    results_lock = threading.Lock()
    threads: list[threading.Thread] = []
    start_wall = time.time()
    try:
        for i in range(concurrency):
            t = threading.Thread(
                target=_run_one,
                args=(ctx, i, cancel_event, step_root / f"slot{i}",
                       results, results_lock, per_job_timeout,
                       expected_seconds, duration_tolerance, registry),
                name=f"bench-{concurrency}-{i}", daemon=True,
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
    finally:
        try: shutil.rmtree(step_root, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
    wall = time.time() - start_wall
    successes = sum(1 for r in results if r["ok"])
    failures = concurrency - successes
    session_hits = oom_hits = dev_hits = hw_hits = 0
    # R14-FIX-3 P1-2：硬件编码器发生 timeout 也应计入 hardware_errors——
    # 之前 stderr="timeout>Xs" 不含 nvenc/qsv/amf 关键字，被 _classify_stderr
    # 判为普通失败，导致 hw_failures_total 恒为 0。
    is_hw_encoder = (ctx.encoder.encoder != "libx264")
    for r in results:
        if not r["ok"]:
            stderr_text = r.get("stderr") or ""
            c = _classify_stderr(stderr_text)
            session_hits += c["session_limit"]
            oom_hits += c["oom"]
            dev_hits += c["device"]
            hw_hits += c["hw"]
            # 硬件 encoder 的 timeout 视为硬件错误
            if is_hw_encoder and c["hw"] == 0 \
                    and stderr_text.startswith("timeout>"):
                hw_hits += 1
    aggregate_fps = 0.0
    if wall > 0:
        # 成功任务的输出帧数（都跑 sample_seconds * sample_fps 帧）
        # 但基准里为了 speed 我们不再单独取 output 帧数；用 sample 长度换算聚合速率
        frames_each = ctx.sample_fps * max(1e-3, min(ctx.sample_fps * 60, 10_000))
        # 保守用 sample_seconds*sample_fps
        try:
            from .ffmpeg_pipeline import ffprobe_video as _pv
            probe = _pv(ctx.input_video)
            frames_each = probe.duration * probe.fps
        except Exception:  # noqa: BLE001
            pass
        aggregate_fps = (successes * frames_each) / wall
    sample_fps = ctx.sample_fps or 30.0
    realtime_multiple = aggregate_fps / sample_fps if sample_fps else 0.0
    # 每小时物理成片数 = 成功数 / 墙钟秒 * 3600
    projected_per_hour = 0.0
    if wall > 0 and successes > 0:
        projected_per_hour = (successes / wall) * 3600.0
    is_recommendable = (
        successes == concurrency
        and hw_hits == 0
        and session_hits == 0
        and oom_hits == 0
        and dev_hits == 0
    )
    notes = ""
    if warmup:
        notes = "warmup"
    if failures:
        notes = (notes + f"; {failures} failures").strip("; ")
    return LadderRunResult(
        concurrency=concurrency,
        successes=successes,
        failures=failures,
        hw_fallbacks=0,
        session_limit_errors=session_hits,
        oom_errors=oom_hits,
        device_errors=dev_hits,
        wall_clock_seconds=round(wall, 3),
        aggregate_fps=round(aggregate_fps, 2),
        realtime_multiple=round(realtime_multiple, 3),
        projected_renders_per_hour=round(projected_per_hour, 2),
        projected_renders_per_day=round(projected_per_hour * 24, 0),
        is_recommendable=is_recommendable,
        # R14-FIX2 P1-2：真正记录硬件错误命中数
        hardware_errors=hw_hits,
        notes=notes,
    )


def choose_recommended_concurrency(steps: list[LadderRunResult]) -> tuple[int, str]:
    """R14 推荐并发选择：

    - 只考虑 is_recommendable == True 的档；
    - 若最高稳定档相对次高稳定档吞吐提升 < IMPROVEMENT_THRESHOLD → 降一档；
    - 若无稳定档 → 推荐 1（CPU-safe 兜底），reason 明示。
    """
    stable = [s for s in steps if s.is_recommendable]
    if not stable:
        return 1, "无任何并发档通过硬件失败 / 会话上限筛选，推荐降到 1（CPU-safe）"
    stable.sort(key=lambda s: s.concurrency)
    # 从最大稳定档回退
    best_idx = len(stable) - 1
    while best_idx > 0:
        cur = stable[best_idx]
        prev = stable[best_idx - 1]
        if prev.projected_renders_per_hour <= 0:
            break
        gain = (cur.projected_renders_per_hour - prev.projected_renders_per_hour) \
            / prev.projected_renders_per_hour
        if gain < IMPROVEMENT_THRESHOLD:
            best_idx -= 1
            continue
        break
    chosen = stable[best_idx]
    if best_idx == len(stable) - 1:
        reason = f"c={chosen.concurrency} 是稳定档最高，且相对上一档吞吐提升 >= {int(IMPROVEMENT_THRESHOLD*100)}%"
    else:
        reason = f"c={chosen.concurrency}：更高档相对提升不足 {int(IMPROVEMENT_THRESHOLD*100)}%，选更稳定的低档"
    return chosen.concurrency, reason


def run_benchmark(*, ffmpeg: str, sample_video: str | Path,
                    sample_tts_audio: str | Path,
                    encoder_preference: str = "auto",
                    zoom_percent: int = 130,
                    keep_original_audio: bool = False,
                    preset: str = "medium", crf: int = 20,
                    ladder: Iterable[int] = DEFAULT_LADDER,
                    warmup_rounds: int = 1,
                    measured_rounds: int = 3,
                    per_job_timeout: float = 300.0,
                    progress_cb: Callable[[dict], None] | None = None,
                    cancel_event: threading.Event | None = None,
                    registry: BenchmarkProcessRegistry | None = None,
                    ) -> DeviceCapability:
    """跑完一次基准，返回带 ladder_results / recommended_concurrency 的 DeviceCapability。

    - **不修改任何生产 scheduler 状态**——由 controller 层负责 pause/resume；
    - staging 落在 `<data_root>/批量带货/staging/_benchmark_<uuid>`，结束后清理；
    - 阶梯出现硬件失败即"提前停止"。
    """
    if cancel_event is None:
        cancel_event = threading.Event()
    sample_video = Path(sample_video)
    sample_tts_audio = Path(sample_tts_audio)
    if not sample_video.is_file():
        raise FileNotFoundError(f"代表样本视频不存在：{sample_video}")
    if not sample_tts_audio.is_file():
        raise FileNotFoundError(f"代表样本 TTS 音频不存在：{sample_tts_audio}")

    from .ffmpeg_pipeline import bulk_staging_root, ffprobe_video
    bench_root = bulk_staging_root() / f"_benchmark_{uuid.uuid4().hex[:8]}"
    bench_root.mkdir(parents=True, exist_ok=True)

    try:
        encoder = resolve_encoder(ffmpeg, preference=encoder_preference)
        probe = ffprobe_video(sample_video)
        cap = detect_capability_metadata(
            ffmpeg, encoder_preference=encoder_preference,
            sample_probe=probe, sample_path=str(sample_video),
        )
        cap.tested_at = time.time()
        cap.tested_ladder = list(ladder)

        ctx = _prepare_bench_context(
            ffmpeg=ffmpeg, input_video=sample_video,
            tts_audio=sample_tts_audio, staging_root=bench_root,
            encoder=encoder, zoom_percent=zoom_percent,
            keep_original_audio=keep_original_audio,
            preset=preset, crf=crf,
        )

        # R14-FIX P1-1：expected_seconds = min(video*2, tts) → 生产同款计算
        try:
            from .ffmpeg_pipeline import ffprobe_seconds
            tts_seconds = ffprobe_seconds(sample_tts_audio)
        except Exception:  # noqa: BLE001
            tts_seconds = 0.0
        expected_out_seconds = min(probe.duration * 2, tts_seconds) if tts_seconds > 0 else (probe.duration * 2)
        cap.sample_seconds = probe.duration
        cap.filter_chain_kind = "mirror_concat_130_v1"

        results: list[LadderRunResult] = []
        stopped_early = False
        m_rounds = max(1, int(measured_rounds))
        for c in ladder:
            if cancel_event.is_set():
                raise BenchmarkCancelled("benchmark cancelled")
            # R14-FIX P1-1 warmup + N 轮正式测量，取中位数
            measured: list[LadderRunResult] = []
            for round_i in range(max(1, warmup_rounds) + m_rounds):
                if cancel_event.is_set():
                    raise BenchmarkCancelled("benchmark cancelled mid-round")
                is_warmup = round_i < warmup_rounds
                r = run_one_ladder_step(
                    ctx, c, cancel_event=cancel_event,
                    per_job_timeout=per_job_timeout,
                    warmup=is_warmup,
                    expected_seconds=expected_out_seconds,
                    registry=registry,
                )
                if is_warmup:
                    if progress_cb:
                        progress_cb({"phase": "warmup", "concurrency": c,
                                       "result": asdict(r)})
                    continue
                measured.append(r)
                if progress_cb:
                    progress_cb({"phase": "measure", "concurrency": c,
                                   "result": asdict(r)})
            # R14-FIX2 P1-1：**真中位数**——按 projected_renders_per_hour 排序
            # 后按 statistics.median 语义取中间元素（奇数 = 中位；
            # 偶数 = 使用较低那半的最大值以保守化推荐，绝不采用最快异常轮）
            assert measured, "at least one measured round"
            import statistics as _st
            ordered = sorted(measured, key=lambda x: x.projected_renders_per_hour)
            n = len(ordered)
            if n % 2 == 1:
                median = ordered[n // 2]
            else:
                # 偶数轮：取较低的中间值（保守），避免异常快轮被采用
                median = ordered[(n // 2) - 1]
            # 同时 sanity check 数字（保留原语义）
            _median_rate = _st.median(
                [x.projected_renders_per_hour for x in measured]
            )
            # is_recommendable = 所有测量轮都通过 才算稳定档
            all_stable = all(x.is_recommendable for x in measured)
            median = LadderRunResult(
                concurrency=median.concurrency,
                successes=median.successes,
                failures=median.failures,
                hw_fallbacks=median.hw_fallbacks,
                session_limit_errors=median.session_limit_errors,
                oom_errors=median.oom_errors,
                device_errors=median.device_errors,
                wall_clock_seconds=median.wall_clock_seconds,
                aggregate_fps=median.aggregate_fps,
                realtime_multiple=median.realtime_multiple,
                projected_renders_per_hour=median.projected_renders_per_hour,
                projected_renders_per_day=median.projected_renders_per_day,
                is_recommendable=all_stable,
                hardware_errors=median.hardware_errors,
                notes=median.notes + (f" | median(n={n})" if n > 1 else ""),
            )
            results.append(median)
            # 累计错误计数（对所有测量轮求和）
            for r in measured:
                # R14-FIX2 P1-2：累加真正的 hardware_errors，而不是恒 0 的 hw_fallbacks
                cap.hw_failures_total += r.hardware_errors
                cap.session_limit_errors_total += r.session_limit_errors
                cap.oom_errors_total += r.oom_errors
            if not all_stable:
                stopped_early = True
                break

        cap.ladder_results = [asdict(r) for r in results]
        stable = [r for r in results if r.is_recommendable]
        cap.max_successful_concurrency = max((r.concurrency for r in stable),
                                                default=0)
        recommended, reason = choose_recommended_concurrency(results)
        cap.recommended_concurrency = recommended
        cap.reason = reason + (" | 已提前停止" if stopped_early else "")
        peak = max((r.projected_renders_per_hour for r in stable), default=0.0)
        cap.peak_renders_per_hour = round(peak, 2)
        cap.peak_renders_per_day = round(peak * 24, 0)
        cap.reached_10k_per_day = cap.peak_renders_per_day >= 10000
        return cap
    finally:
        try: shutil.rmtree(bench_root, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 生成 10 秒代表样本 TTS（真实基准需要真实音频）
# --------------------------------------------------------------------------


def make_silent_wav(target: Path, seconds: float = 10.0,
                     sample_rate: int = 16000) -> Path:
    """生成 N 秒静音 wav，仅作为基准 TTS 输入占位——
    真实生产走 EdgeTtsBackend；这里只需要一个真实合法 wav 让 FFmpeg 有音频源。"""
    import wave
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * sample_rate)
    with wave.open(str(target), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frames)
    return target
