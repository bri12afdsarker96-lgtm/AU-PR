"""配音对齐工作室——浏览器版界面的本地后端（纯标准库，零新依赖）。

沿用水星 api_server 的口径：http.server 薄封装、默认只绑 127.0.0.1、
长任务异步 + 轮询。本服务是**本机桌面 UI 的后端**（非对外 API），
浏览器页面（web/index.html）与其同源提供。

端点：
    GET  /                     → 界面（web/index.html）
    GET  /api/state            → 组件探测 + 音色列表 + 常量（引擎/尺子/位置预设）
    POST /api/voices?name=&transcript=&filename= → 原始音频字节上传并登记音色
    DELETE /api/voices/<id>    → 删除音色
    POST /api/run              → {action: run_all|dub|timing|render|capcut, ...} 开任务
    GET  /api/job              → {running, done, ok, log:[...]} 轮询
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import components as toolbox
from . import edit_queue
from . import fonts as font_library
from . import settings as studio_settings
from . import studio_pipeline as pipeline
from . import voice_library
from . import xlsx_reader
from .aligners import WhisperAligner
from .engines import (
    DotsLocalEngine,
    DotsRemoteEngine,
    EdgeTtsEngine,
    FishLocalEngine,
    MockEngine,
    SynthesisOptions,
)
from .engines import edge_tts as edge_tts_mod
from .overlays import POSITION_PRESETS, overlays_from_dicts
from .subtitles import SubtitleStyle

STUDIO_HOME: Path | None = None  # 测试覆盖用；None = 走设置的总目录/音色库
DEFAULT_PORT = 8760


def _vroot() -> Path:
    return STUDIO_HOME if STUDIO_HOME is not None else studio_settings.voices_library_root()
def _index_path() -> Path:
    """界面文件：PyInstaller 冻结包内落在 _MEIPASS/dub_align_studio/web。"""
    import sys

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = base / "dub_align_studio" / "web" / "index.html"
        if candidate.exists():
            return candidate
    return Path(__file__).parent / "web" / "index.html"


_INDEX = _index_path()
_MAX_UPLOAD = 200 * 1024 * 1024  # 参考音频上限 200MB，足够富余


# ------------------------------------------------------------------ 任务状态（多槽并发）
# 槽位规则：主流程（配音/量时长/渲染/剪映/自检…）共用 main 槽保持互斥；
# 每个组件/字体下载各占独立槽 → 并发下载互不阻塞，也不再挡住主流程与自检。
@dataclass
class JobState:
    slot: str = "main"
    label: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    done: bool = False
    ok: bool = False
    action: str = ""
    log: list[str] = field(default_factory=list)
    timings: list | None = None
    result: object | None = None
    stage: str = ""          # 当前阶段文字（生成成片进度用）
    progress: int = 0        # 0~100 进度百分比
    cancelled: bool = False  # 队列任务取消标志（进度回调检查后中止）
    last_tick: float = 0.0   # 最后一次进度更新时刻（看门狗判「疑似卡死」用）

    def check_cancel(self) -> None:
        """协作式取消：run_all 各步进度回调调用它，一旦被取消就抛出中止。"""
        if self.cancelled:
            raise RuntimeError("已取消该任务")

    def snapshot(self) -> dict:
        with self.lock:
            timings = [
                {"index": t.index, "text": t.text, "duration": t.duration}
                for t in (self.timings or [])
            ]
            try_audio = ""
            if isinstance(self.result, dict):
                try_audio = str(self.result.get("try_audio") or "")
            return {"id": self.slot, "label": self.label,
                    "running": self.running, "done": self.done, "ok": self.ok,
                    "action": self.action, "log": list(self.log), "timings": timings,
                    "try_audio": try_audio, "stage": self.stage, "progress": self.progress}

    def append(self, message: str) -> None:
        with self.lock:
            self.log.append(message)

    def set_progress(self, stage: str, percent: int) -> None:
        with self.lock:
            self.stage = stage
            self.progress = max(0, min(100, int(percent)))
            self.last_tick = time.time()


JOB = JobState()          # main 槽（保持旧口径：/api/job 即它）
TASKS: dict[str, JobState] = {"main": JOB}
TASKS_LOCK = threading.Lock()


def _slot_for(action: str, payload: dict) -> tuple[str, str]:
    """action → (槽位, 展示名)。组件/字体/fish 服务各自独立槽，其余共用 main。"""
    if action == "component":
        key = str(payload.get("component_key") or "")
        item = next((c for c in toolbox.COMPONENTS if c["key"] == key), None)
        return f"component:{key}", str(item["name"]) if item else key
    if action == "font":
        key = str(payload.get("font_key") or "")
        pack = next((f for f in font_library.FONT_PACK if f["key"] == key), None)
        return f"font:{key}", ("字体·" + str(pack["name"])) if pack else key
    if action in {"fish_server", "fish_server_stop"}:
        return "fish_server", "fish-speech 服务"
    return "main", action


def _start_task(action: str, payload: dict) -> tuple[JobState | None, str]:
    """占槽并启动后台任务；槽位忙时返回 (None, 提示)。"""
    slot, label = _slot_for(action, payload)
    with TASKS_LOCK:
        current = TASKS.get(slot)
        if current is not None:
            with current.lock:
                if current.running:
                    return None, f"「{current.label or current.action}」正在执行，请稍候（其他下载/操作可并行）。"
        if slot == "main":
            job = JOB
            with job.lock:
                job.running, job.done, job.ok = True, False, False
                job.action, job.label = action, label
                job.log = [f"══ {action} 开始 ══"]
                job.stage, job.progress = "", 0
                # 清掉上一次任务的残留，避免串味：例如试听(voice_try)把 result 设成 dict，
                # 随后「导出剪映草稿」会拿它当 DubBResult 取 .shots 崩；或上一集的 timings 套到
                # 新输出目录。清空后各 action 走磁盘回读(load_timings/分镜段)，按当前输出目录取。
                job.timings, job.result, job.cancelled = None, None, False
        else:
            job = JobState(slot=slot, label=label, running=True, action=action,
                           log=[f"══ {label} 开始 ══"])
            TASKS[slot] = job
    threading.Thread(target=_run_job, args=(job, action, payload), daemon=True).start()
    return job, ""


def _remember_edit_item(output_dir: Path, canvas: tuple[int, int], payload: dict) -> None:
    """成片烧完字幕后登记进「待编辑队列」（⑧），供文本框二次精修选取。失败不阻塞成片。

    连同生成时的烧录设置一并存下（B），文本框选中它时可原样还原，重烧与首次一致。"""
    try:
        film = output_dir / pipeline.FILM_NAME
        if not film.is_file():
            return
        settings = {
            "sub": {"size": payload.get("subtitle_size"), "pos": payload.get("subtitle_position"),
                    "font": payload.get("subtitle_font"), "color": payload.get("subtitle_color"),
                    "border": payload.get("subtitle_border")},
            "burn": bool(payload.get("burn_subtitles", True)),
            "pb": payload.get("progressbar") or {},
            "wm": payload.get("watermark") or {},
            "overlays": payload.get("overlays") or [],
            "audio": payload.get("audio") or {},
            "aspect": payload.get("aspect") or "",
            # 重烧要按**这条成片原本的分镜来源**重新取画面（flat 模式不复用选片清单），
            # 否则会套到当前界面里别的一集的画面。故一并存 shots_dir/素材模式/seed。
            "shots_dir": str(payload.get("shots_dir") or ""),
            "material_mode": str(payload.get("material_mode") or "flat"),
            "seed": payload.get("seed"),
        }
        edit_queue.add(str(payload.get("title") or output_dir.name),
                       str(film), str(output_dir), canvas, settings)
    except Exception:
        pass


# ------------------------------------------------------------------ 生成任务队列（⑨ 串行后台）
# ＋加入队列：把当前配置**整份冻结**成一个任务追加到队列；一个后台 worker 顺序取任务跑
# run_all（流水线：配音串行·GPU 不抢，渲染串行；两阶段跨任务重叠→A 渲染时 B 配音）。跑队列期间用户可照常改设置/提交下一个/去文本框精修
# 别的成片——已入队任务的 payload 已深拷冻结，互不影响（用户 2026-07-26 选 A 方案）。
GEN_COND = threading.Condition()      # 同时充当 GEN_QUEUE 的锁
GEN_QUEUE: list[dict] = []            # 每项：{id,title,payload,job:JobState,status,error}
GEN_PAUSED = False                    # 队列暂停：不再启动新任务（正在跑的那个不受影响）
STUCK_SECONDS = 240                   # 运行中任务超过此秒无「阶段心跳」→ 前端温和提示、可手动取消
                                      # （已在模型加载/每行开始处打心跳，故只有单行/加载真的异常久才触发）
# 流水线：配音(GPU/云) 与 量时长+渲染(本地 CPU) 用不同资源 → 两级串行锁，允许「上一个任务渲染时
# 下一个任务配音」并行、吞吐更高；仍保证「一次只一个配音」(GPU 不抢) 和「一次只一个渲染」。
_DUB_LOCK = threading.Lock()          # 配音阶段串行锁
_RENDER_LOCK = threading.Lock()       # 量时长+渲染阶段串行锁
_GEN_WORKERS: list = []
_GEN_WORKER_COUNT = 2                 # 2 个 worker = 流水线深度 2（一个配音、一个渲染重叠）


class _PhaseLock:
    """阶段串行锁的上下文管理器：**等锁期间持续刷新心跳并响应取消**——否则 B 等 A 配完那段时间
    (可能 > STUCK_SECONDS) 会被误判「疑似卡死」。进入即阻塞获取，退出即释放。"""

    def __init__(self, lock: threading.Lock, job: "JobState") -> None:
        self._lock = lock
        self._job = job

    def __enter__(self) -> "_PhaseLock":
        while not self._lock.acquire(timeout=5):
            self._job.check_cancel()            # 等锁期间也能被取消中止
            with self._job.lock:
                self._job.last_tick = time.time()   # 刷新心跳，等锁不算卡死
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()


def _next_pending_locked():
    """持锁下取下一个待办；队列暂停时返回 None（不启动新任务）。"""
    if GEN_PAUSED:
        return None
    return next((e for e in GEN_QUEUE if e["status"] == "pending"), None)


def _gen_enqueue(payload: dict, title: str) -> str:
    """深拷冻结 payload、建任务、唤醒 worker；返回任务 id。"""
    task_id = uuid.uuid4().hex[:8]
    job = JobState(slot=f"queue:{task_id}", label=title, action="run_all")
    entry = {"id": task_id, "title": title, "payload": copy.deepcopy(payload),
             "job": job, "status": "pending", "error": "",
             "retries": 0, "auto_max": 1}   # 失败自动重试 1 次；仍失败则跳过、留手动「重试」
    with GEN_COND:
        GEN_QUEUE.append(entry)
        _ensure_gen_worker_locked()
        GEN_COND.notify()
    return task_id


def _ensure_gen_worker_locked() -> None:
    """在持锁状态下确保 _GEN_WORKER_COUNT 个 worker 存活（流水线并行：配音与渲染重叠）。"""
    global _GEN_WORKERS
    _GEN_WORKERS = [t for t in _GEN_WORKERS if t.is_alive()]
    while len(_GEN_WORKERS) < _GEN_WORKER_COUNT:
        t = threading.Thread(target=_gen_worker_loop, name=f"gen-queue-{len(_GEN_WORKERS)+1}", daemon=True)
        t.start()
        _GEN_WORKERS.append(t)


def _gen_worker_loop() -> None:
    while True:
        with GEN_COND:
            entry = _next_pending_locked()
            while entry is None:
                GEN_COND.wait()                       # 无待办 / 已暂停 → 休眠，enqueue/继续时唤醒
                entry = _next_pending_locked()
            entry["status"] = "running"
            job: JobState = entry["job"]
            payload = entry["payload"]
            attempt = entry["retries"] + 1
            with job.lock:
                job.running, job.done, job.ok, job.cancelled = True, False, False, False
                job.log = [f"══ 队列任务「{entry['title']}」开始克隆" + (f"（第 {attempt} 次尝试）" if attempt > 1 else "") + " ══"]
                job.stage, job.progress, job.last_tick = "", 0, time.time()
        # 单个任务出问题绝不掀翻整队：_run_job 内部已兜异常；这里再包一层防线程被杀。
        try:
            _run_job(job, "run_all", payload)         # 复用整套 run_all（含成功后进待编辑队列）
        except Exception as exc:                       # noqa: BLE001 —— worker 必须存活
            job.append("❌ 失败：" + "".join(traceback.format_exception_only(exc)).strip())
            with job.lock:
                job.running, job.done, job.ok = False, True, False
        with GEN_COND:
            if job.ok:
                entry["status"] = "done"              # 已成功优先：取消若在收尾后到达，不误标已取消
            elif job.cancelled:
                entry["status"] = "cancelled"         # 用户取消 → 跳过，不自动重试
                entry["error"] = "已取消"
            elif entry["retries"] < entry["auto_max"]:
                entry["retries"] += 1                  # 自动重试：重置为待办，worker 稍后再取
                entry["job"] = JobState(slot=job.slot, label=entry["title"], action="run_all")
                entry["status"] = "pending"
                GEN_COND.notify()
            else:
                entry["status"] = "failed"             # 跳过这个任务，继续队列后续
                entry["error"] = next(
                    (m for m in reversed(job.log) if "失败" in m or "错误" in m), "克隆失败")


def _gen_queue_snapshot() -> dict:
    now = time.time()
    with GEN_COND:
        out = []
        for e in GEN_QUEUE:
            job: JobState = e["job"]
            with job.lock:
                idle = int(now - job.last_tick) if (e["status"] == "running" and job.last_tick) else 0
                out.append({"id": e["id"], "title": e["title"], "status": e["status"],
                            "error": e["error"], "stage": job.stage, "progress": job.progress,
                            "running": job.running, "ok": job.ok, "retries": e.get("retries", 0),
                            "output_dir": str((e.get("payload") or {}).get("output_dir") or ""),
                            "idle_seconds": idle, "stuck": bool(idle >= STUCK_SECONDS),
                            "log": list(job.log)[-40:]})
        return {"tasks": out, "paused": GEN_PAUSED, "stuck_after": STUCK_SECONDS}


def _gen_queue_set_paused(paused: bool) -> None:
    global GEN_PAUSED
    with GEN_COND:
        GEN_PAUSED = bool(paused)
        if not GEN_PAUSED:
            _ensure_gen_worker_locked()                # 继续前确保 worker 存活（万一曾意外退出）
        GEN_COND.notify_all()                          # 继续时唤醒 worker 取下一个


def _gen_queue_cancel(task_id: str) -> bool:
    """取消/删除一个任务：待办直接移除；运行中置取消标志（进度间隙协作中止）。"""
    with GEN_COND:
        for i, e in enumerate(GEN_QUEUE):
            if e["id"] != task_id:
                continue
            if e["status"] == "pending":
                GEN_QUEUE.pop(i)
                return True
            if e["status"] == "running":
                e["job"].cancelled = True              # run_all 进度回调 check_cancel 抛错中止
                e["job"].append("⏹ 收到取消指令，正在中止…")
                return True
            return False
    return False


def _gen_queue_retry(task_id: str) -> bool:
    """手动重试一个已失败（或已完成）的任务：重置为待办、清零错误，唤醒 worker。"""
    with GEN_COND:
        for e in GEN_QUEUE:
            if e["id"] == task_id and e["status"] in ("failed", "done", "cancelled"):
                # 重试自动复用已有配音：配音成功后才会有 master.wav，若失败在后续步骤（如渲染缺 ffmpeg），
                # 重试应跳过重新克隆、直接量时长+渲染（省时间/云端算力）。配音本身没成功则无 master.wav，
                # run_all 里 reuse_dub 有 master_path.is_file() 兜底会自动失效、照常克隆——故此处置 True 安全。
                if isinstance(e.get("payload"), dict):
                    e["payload"]["reuse_dub"] = True
                e["job"] = JobState(slot=f"queue:{task_id}", label=e["title"], action="run_all")
                e["status"], e["error"], e["retries"] = "pending", "", 0
                _ensure_gen_worker_locked()            # 重试前确保 worker 存活
                GEN_COND.notify()
                return True
    return False


def _gen_queue_remove(task_id: str) -> bool:
    """移除一个**尚未开始**的队列任务（运行中/已完成不动）。"""
    with GEN_COND:
        for i, e in enumerate(GEN_QUEUE):
            if e["id"] == task_id and e["status"] == "pending":
                GEN_QUEUE.pop(i)
                return True
    return False


def _gen_queue_clear_finished() -> int:
    """清掉已完成/失败的任务项（运行中/待办保留）。"""
    with GEN_COND:
        before = len(GEN_QUEUE)
        GEN_QUEUE[:] = [e for e in GEN_QUEUE if e["status"] in ("pending", "running")]
        return before - len(GEN_QUEUE)


#: 只有**真实调用远程 GPU 合成**的 action 才刷 cloud_gpu 空闲计时——
#: 本地 timing/render/finalize/capcut/premiere/cleanup 都是 CPU 上跑的，
#: 界面即使选着 dots_remote 也不代表这些动作用了云 GPU；不能让它们假装"活跃"
#: 而阻断空闲自动关。voice_try 单独一句试听算真实合成。
_CLOUD_GPU_SYNTH_ACTIONS = frozenset({"dub", "rechunk", "voice_try", "run_all"})


def _uses_cloud_gpu(action: str, payload: dict) -> bool:
    """本 action 若真调 dots_remote 合成，才返回 True。

    - **本地阶段** action（timing/render/finalize/capcut/premiere/cleanup 等）
      即使 payload.engine=dots_remote 也**不算**——它们不打云 GPU。
    - `run_all` 只在进入配音阶段前判断为 True；若 reuse_dub=True 且 master 已存在
      → 跳过远程合成 → 本函数入口处**无法**判定，此时由调用点显式判断（见
      _run_job 内 run_all 分支：只在真的要合成前才手动 mark_active）。
    - 引擎不是 dots_remote（含 edge_tts / mock / 本地）永远返回 False。"""
    if action not in _CLOUD_GPU_SYNTH_ACTIONS:
        return False
    return pipeline.is_cloud_gpu_engine(str(payload.get("engine") or ""))


def _mark_cloud_gpu_active_if_needed(action: str, payload: dict) -> None:
    """按 action+engine 决定是否刷新 cloud_gpu 空闲计时。**通用入口**——
    专门给 run_all 里"真正开始配音"的时刻显式调用请用 `mark_cloud_gpu_active_now`。"""
    if not _uses_cloud_gpu(action, payload):
        return
    mark_cloud_gpu_active_now()


def mark_cloud_gpu_active_now() -> None:
    """无条件刷新一次云 GPU 活跃时间——供 run_all 内部在**确认真的要走远程合成**
    的位置（entering dots_remote 阶段 / heartbeat / 配音完成）显式调用。"""
    try:
        from . import cloud_gpu
        cloud_gpu.manager().mark_active()
    except Exception:  # noqa: BLE001
        pass


def _run_job(JOB: JobState, action: str, payload: dict) -> None:  # noqa: N803 —— 沿用旧函数体的 JOB 名
    log = JOB.append
    # 云 GPU 保活：**不在入口无条件刷新**——那样 run_all+reuse_dub、voice_try 命中缓存、
    # 参数校验失败、找不到分段等"根本没真调远程"的场景都会被误算成活跃，阻塞空闲自动关。
    # 只有在 dub/rechunk/voice_try/run_all 里**真正进入 dots_remote 请求**的那一刻，
    # 才由各分支自行显式调 `mark_cloud_gpu_active_now`（heartbeat + progress + 结束都续期）。
    try:
        text = str(payload.get("text") or "")
        output_dir = Path(str(payload.get("output_dir") or "")) if payload.get("output_dir") else None
        engine_key = str(payload.get("engine") or "mock")
        aligner_key = str(payload.get("aligner") or "均分兜底")
        voice = None
        vparams: dict = {}
        # Edge TTS 只用预设声线，**不查音色库**（避免和克隆音色混淆、也不制造空 VoiceRef）
        if engine_key != "edge_tts" and payload.get("voice_id"):
            entry = voice_library.get_voice(_vroot(), str(payload["voice_id"]))
            voice = entry.to_ref()
            vparams = entry.params or {}
        # 合成参数：所选音色设计的 num_steps/guidance 生效于成片；速度/种子/停顿以界面为准。
        # P0-3：**严格区分** None/空串（用默认）与数字 0（合法但对 speed 是错误）——
        # 旧写法 `payload.get("speed") or vparams.get("speed") or 1.0` 会把 0 吞成 1.0。
        options = SynthesisOptions(
            num_steps=int(_as_number(payload.get("num_steps"),
                                     _as_number(vparams.get("num_steps"), 10))),
            guidance_scale=float(_as_number(payload.get("guidance_scale"),
                                            _as_number(vparams.get("guidance_scale"), 1.2))),
            speed=float(_as_number(payload.get("speed"),
                                    _as_number(vparams.get("speed"), 1.0))),
            max_pause_seconds=float(_as_number(payload.get("max_pause"), 0.0)),
            seed=int(_as_number(payload.get("seed"), _as_number(vparams.get("seed"), 42))),
            edge_voice=str(payload.get("edge_voice") or ""),
            edge_pitch=int(_as_number(payload.get("edge_pitch"), 0)),
            edge_style=str(payload.get("edge_style") or "general"),
        )
        style = None
        if payload.get("burn_subtitles", True):
            style = SubtitleStyle(
                font_size_px=int(payload.get("subtitle_size") or 64),
                position=str(payload.get("subtitle_position") or "底部"),
                font_name=str(payload.get("subtitle_font") or ""),
                color=str(payload.get("subtitle_color") or "white"),
                border_width=int(payload.get("subtitle_border") if payload.get("subtitle_border") not in (None, "") else 3),  # 容 null/""，但保留 0（无描边）
            )
        overlays = overlays_from_dicts(payload.get("overlays") or [])
        from .progressbar import progressbar_from_payload
        from .watermark import watermark_from_payload

        progress_bar = progressbar_from_payload(payload)  # 未启用返回 None
        watermark = watermark_from_payload(payload)       # 未启用返回 None
        aspect = str(payload.get("aspect") or pipeline.DEFAULT_ASPECT)
        config = pipeline.make_render_config(aspect)
        canvas = (config.width, config.height)
        from .audio_mix import mix_from_payload

        # 收集缺失素材的诊断信息 → 日志打黄色警告（不阻塞成片；避免"设置了却没声"的静默假成功）
        mix_warnings: list[str] = []
        audio_mix = mix_from_payload(payload.get("audio") or {}, _resolve_asset,
                                      warnings=mix_warnings)
        for w in mix_warnings:
            log(f"⚠ {w}")
        # P1（汇总条位置）：**仅**在真正产生成片且 result.ok=True 时输出汇总。
        # 由各 render-产出分支自行调用 `_log_mix_summary`；finally 里绝不输出——
        # 否则会误报：仅配音/试听/探测不产生成片、或成片渲染失败时报"成片中不包含
        # 这些声音"，但根本没有"成片"。
        _mix_warning_count = len(mix_warnings)

        def _log_mix_summary_if_needed() -> None:
            if _mix_warning_count > 0:
                log(f"⚠ 本次共有 {_mix_warning_count} 个音频素材被跳过，成片中不包含这些声音。")

        # 友好校验：目录留空时给明确提示，避免 Path(None) 抛 TypeError（用户反馈①）
        if action in ("run_all", "dub", "timing", "render", "capcut", "rechunk", "finalize", "premiere", "cleanup") and output_dir is None:
            raise ValueError("请先在下方选择「输出目录」（配音与成片都写到这里）。")
        if action in ("run_all", "render", "finalize") and not str(payload.get("shots_dir") or "").strip():
            raise ValueError("请先选择「分镜目录」（放 1.mp4、2.mp4 … 的文件夹）。")
        if action in ("run_all", "dub", "timing") and not text.strip():
            raise ValueError("请先填写或导入「待合成文案」。")

        material_mode = str(payload.get("material_mode") or "flat")
        if action == "run_all":
            from integrated_workbench.semantic_match import parse_script

            JOB.check_cancel()   # 开跑前先看是否已被取消（队列任务）
            shots_dir = Path(str(payload.get("shots_dir") or ""))
            lines = parse_script(text)
            log(f"素材模式：{ {'flat':'平铺顺序','folder_order':'文件夹顺序','keyword':'关键字匹配'}.get(material_mode, material_mode) }")
            videos = pipeline.select_shot_videos(shots_dir, lines, material_mode,
                                                 int(payload.get("seed") or 42), output_dir, log=log)

            master_path = output_dir / pipeline.MASTER_NAME
            reuse_dub = bool(payload.get("reuse_dub")) and master_path.is_file()
            if not reuse_dub:
                JOB.set_progress("① 配音 · 排队中（等待配音槽）…", 5)
            with _PhaseLock(_DUB_LOCK, JOB):   # 配音阶段串行：GPU 一次只跑一个克隆；期间上一个任务可并行渲染
                if reuse_dub:
                    # 复用已有配音：只改了音量/BGM/字幕/进度条时，跳过整段克隆，直接量时长+重渲染（秒出）
                    from .engines.base import wav_seconds

                    JOB.set_progress("① 复用已有配音（跳过克隆）", 76)
                    log(f"① 复用已有配音：master.wav 已存在（{wav_seconds(master_path):.2f}s），跳过克隆，仅重渲染。")
                else:
                    JOB.set_progress("① 配音 · 逐行克隆", 5)
                    log("① 配音 · 逐行克隆…")
                    _uses_remote_gpu = pipeline.is_cloud_gpu_engine(engine_key)
                    # P0-4：**不在分支入口 mark**——保活钩子由 synthesize_long 在紧邻
                    # engine.synthesize_full 的位置调用；after 放 finally，成功/失败都刷。
                    # dub_progress/heartbeat 仍保留（UI 用），但不再刷云 GPU。

                    def _dub_progress(done: int, total: int) -> None:
                        JOB.check_cancel()
                        pct = 5 + int(done / max(1, total) * 73)
                        JOB.set_progress(f"① 配音 · 第 {done}/{total} 行", pct)

                    def _dub_beat(stage: str = "") -> None:
                        JOB.check_cancel()
                        with JOB.lock:
                            if stage:
                                JOB.stage = stage
                            JOB.last_tick = time.time()

                    _before = mark_cloud_gpu_active_now if _uses_remote_gpu else None
                    _after = mark_cloud_gpu_active_now if _uses_remote_gpu else None
                    master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                               progress=_dub_progress, heartbeat=_dub_beat,
                                               before_engine_call=_before,
                                               after_engine_call=_after)
                    log(f"  ✅ master {master.seconds:.2f}s（引擎 {master.engine}）")

            JOB.check_cancel()   # 配音后、量时长前的取消检查点
            JOB.set_progress("② 量时长 · 排队中（等待渲染槽）…", 77)
            with _PhaseLock(_RENDER_LOCK, JOB):   # 量时长+渲染阶段串行：本地 ffmpeg/whisper 一次只一个；期间下个任务可并行配音
                JOB.set_progress("② 量时长 · 逐行对齐", 78)
                log("② 量时长 · 逐行对齐…")
                timings, notes = pipeline.step_timing(text, master_path, aligner_key, output_dir)
                for note in notes:
                    log(f"  ⚠ {note}")
                with JOB.lock:
                    JOB.timings = timings

                JOB.set_progress("③ 渲染成片 · 逐行收口", 80)
                log("③ 渲染成片 · 逐行裁剪/变速 + 整轨叠加…")

                def _render_progress(done: int, total: int) -> None:
                    JOB.check_cancel()   # 逐段渲染间隙响应取消
                    pct = 80 + int(done / max(1, total) * 15)  # 渲染占 80~95%（比配音快很多）
                    JOB.set_progress(f"③ 渲染成片 · 第 {done}/{total} 段", pct)

                result = pipeline.step_render(master_path, timings, videos, output_dir, style,
                                              config=config, overlays=overlays, audio_mix=audio_mix,
                                              progress=_render_progress, progress_bar=progress_bar, watermark=watermark)
                log(f"  字幕/文本框：{result.subtitle_note or '未启用'}")
                capcut = None
                if payload.get("export_capcut"):
                    JOB.set_progress("④ 导出剪映草稿", 96)
                    log("④ 导出剪映草稿…")
                    capcut = pipeline.step_capcut(timings, result, master_path, output_dir, style,
                                                  canvas=canvas)
                    log(f"  剪映：{capcut.message}")
                JOB.set_progress("完成", 100)
                log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
                if result.ok:
                    _log_mix_summary_if_needed()   # 仅成片成功时才输出"共 N 个素材被跳过"
                with JOB.lock:
                    JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
                if result.ok:
                    _remember_edit_item(output_dir, canvas, payload)  # ⑧ 进待编辑队列
        elif action == "dub":
            _uses_remote_gpu = pipeline.is_cloud_gpu_engine(engine_key)

            def _dub_progress(done: int, total: int) -> None:
                JOB.set_progress(f"配音 · 第 {done}/{total} 行", int(done / max(1, total) * 100))

            def _dub_beat(stage: str = "") -> None:
                with JOB.lock:
                    if stage:
                        JOB.stage = stage
                    JOB.last_tick = time.time()

            # P0-4：保活钩子紧邻 engine.synthesize_full（after 放 finally）
            _before = mark_cloud_gpu_active_now if _uses_remote_gpu else None
            _after = mark_cloud_gpu_active_now if _uses_remote_gpu else None
            master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                       progress=_dub_progress, heartbeat=_dub_beat,
                                       before_engine_call=_before,
                                       after_engine_call=_after)
            log(f"✅ master：{master.path.name}（{master.seconds:.2f}s，引擎 {master.engine}）")
            with JOB.lock:
                JOB.ok = True
        elif action == "rechunk":
            # 只重配某一段：重合成该段 → 重拼 master（事务式）
            from .engines.longform import redub_chunk

            engine = pipeline.make_engine(engine_key)
            index = int(_as_number(payload.get("chunk_index"), 0))
            _uses_remote_gpu = pipeline.is_cloud_gpu_engine(engine_key)
            # P0-4：**不在分支入口 mark**——manifest 缺失/段号错误也别刷。
            # 钩子由 redub_chunk 在验证通过、真正调 synthesize_full 前后触发。
            _before = mark_cloud_gpu_active_now if _uses_remote_gpu else None
            _after = mark_cloud_gpu_active_now if _uses_remote_gpu else None
            redub_chunk(engine, output_dir / pipeline.MASTER_NAME, index, voice, options, log=log,
                        before_engine_call=_before, after_engine_call=_after)
            with JOB.lock:
                JOB.ok = True
        elif action == "timing":
            timings, notes = pipeline.step_timing(text, output_dir / pipeline.MASTER_NAME,
                                                  aligner_key, output_dir)
            for note in notes:
                log(f"⚠ {note}")
            for item in timings:
                log(f"  {item.index:>2} | {item.duration:>6.2f}s | {item.text[:18]}")
            log("✅ 计时表已写入输出目录。")
            with JOB.lock:
                JOB.timings, JOB.ok = timings, True
        elif action == "render":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            videos = pipeline.select_shot_videos(Path(str(payload.get("shots_dir") or "")),
                                                 [t.text for t in timings], material_mode,
                                                 int(payload.get("seed") or 42), output_dir, log=log)

            def _render_progress(done: int, total: int) -> None:
                JOB.set_progress(f"渲染成片 · 第 {done}/{total} 段", int(done / max(1, total) * 100))

            result = pipeline.step_render(output_dir / pipeline.MASTER_NAME, timings, videos,
                                          output_dir, style, config=config, overlays=overlays,
                                          audio_mix=audio_mix, progress=_render_progress,
                                          progress_bar=progress_bar, watermark=watermark)
            log(f"字幕/文本框：{result.subtitle_note or '未启用'}")
            log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
            if result.ok:
                _log_mix_summary_if_needed()
            with JOB.lock:
                JOB.result, JOB.ok = result, result.ok
        elif action == "finalize":
            # 文本框调完样式后：按当前样式重烧字幕成片。**不再自动导出剪映草稿**
            # （2026-07-25 用户定案：没点「导出剪映草稿」就不生成草稿包）——草稿/工程各有独立按钮。
            master_path = output_dir / pipeline.MASTER_NAME
            timings = JOB.timings or pipeline.load_timings(output_dir)
            # 与首次成片同一份选片（选片清单.csv 复用）——重烧字幕不换画面
            videos = pipeline.select_shot_videos(Path(str(payload.get("shots_dir") or "")),
                                                 [t.text for t in timings], material_mode,
                                                 int(payload.get("seed") or 42), output_dir, log=log)

            def _fin_progress(done: int, total: int) -> None:
                JOB.set_progress(f"重烧字幕 · 第 {done}/{total} 段", 5 + int(done / max(1, total) * 90))

            JOB.set_progress("重烧字幕 · 逐行收口", 5)
            log("按当前样式重烧字幕成片…（未点「导出剪映草稿」不会生成草稿包）")
            result = pipeline.step_render(master_path, timings, videos, output_dir, style,
                                          config=config, overlays=overlays, audio_mix=audio_mix,
                                          progress=_fin_progress, progress_bar=progress_bar, watermark=watermark)
            log(("✅ 成片：" if result.ok else "❌ 收口未过：") + str(result.output_path))
            JOB.set_progress("完成", 100)
            if result.ok:
                _log_mix_summary_if_needed()
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
            if result.ok:
                _remember_edit_item(output_dir, canvas, payload)  # ⑧ 刷新待编辑队列时效
        elif action == "capcut":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            # JOB.result 为 None（软件重启后）时由 step_capcut 从磁盘读回分镜段；
            # v0.7.71 P1-1：A1 走"成片同款混音单轨"，需 output_dir/成片.mp4 已生成
            package = pipeline.step_capcut(timings, JOB.result, output_dir / pipeline.MASTER_NAME,
                                           output_dir, style or SubtitleStyle(), canvas=canvas,
                                           film_mp4=output_dir / pipeline.FILM_NAME)
            log(f"✅ {package.message}")
            log(f"   交接包：{package.package_dir}")
            with JOB.lock:
                JOB.ok = True
        elif action == "voice_release":
            vid = str(payload.get("voice_id") or "")
            released = bool(payload.get("released"))
            voice_library.set_released(_vroot(), vid, released)
            log(("✅ 已发行音色：" if released else "✅ 已取消发行：") + vid +
                ("（成片页音色下拉将置顶展示）" if released else ""))
            with JOB.lock:
                JOB.ok = True
        elif action == "voice_batch":
            folder = str(payload.get("folder") or "").strip()
            if not folder:
                raise ValueError("请先选择要批量导入的文件夹（每个音频=一个音色，文件名即音色名；同名 .txt 自动作为转写）。")
            names = voice_library.batch_import_folder(_vroot(), Path(folder),
                                                      engine=str(payload.get("engine") or "dots_local"))
            for n in names:
                log(f"  ✅ 已建音色：{n}")
            log(f"✅ 批量导入完成，共 {len(names)} 个音色（默认为草稿，确认后到音色库点「发行」）。")
            with JOB.lock:
                JOB.ok = True
        elif action == "premiere":
            # Premiere 交接包：V1=逐行分镜段 A1=整轨配音；XML+SRT+素材全在输出目录
            from .premiere_xml import export_premiere_project
            from .frames import quantize_to_frames
            from .engines.base import wav_seconds

            timings = JOB.timings or pipeline.load_timings(output_dir)
            segments = pipeline.segments_from_output(output_dir, len(timings))
            master_path = output_dir / pipeline.MASTER_NAME
            # 帧数必须与渲染时完全一致：quantize_to_frames 把逐行舍入残差并入末段，Σ帧=round(master*fps)。
            # 若像旧版那样各行独立 round(dur*fps)，声明帧数会与真实分镜段/音频错位（尾部漂移几帧）。
            frames = quantize_to_frames([t.duration for t in timings], config.fps,
                                        wav_seconds(master_path))
            xml_path = export_premiere_project(output_dir, segments, master_path, frames,
                                               config.fps, config.width, config.height,
                                               film_mp4=output_dir / pipeline.FILM_NAME)
            log(f"✅ Premiere 交换工程已导出：{xml_path}")
            log("   ⚠ 用 Premiere「文件 → 导入」选该 .xml（不是「打开项目」——打开只认 .prproj，会提示格式不正确）。")
            log("   导入后即得完整时间线（V1 分镜段·去音轨 + A1 混音单轨=成片同款）；字幕另导入 成片.srt。")
            log("   ⓘ A1 = mixdown.wav 是「成片同款混音单轨」，与成片音效完全一致；BGM/SFX 暂不承诺独立可编辑轨道。")
            with JOB.lock:
                JOB.ok = True
        elif action == "cleanup":
            # 清理缓存：删掉成片生成过程中的可再生中间产物，保留成片与各交接包（2026-07-25 用户需求）
            if not (output_dir / pipeline.FILM_NAME).is_file():
                raise ValueError(f"未找到成片（{pipeline.FILM_NAME}），请先成功生成成片再清理，避免误删。")
            deleted, freed = pipeline.cleanup_intermediates(output_dir)
            if deleted:
                log(f"✅ 已清理中间产物：{'、'.join(deleted)}，释放 {freed / 1024 / 1024:.1f} MB。")
                log("   保留：成片.mp4/.srt、master.wav、配音计时表.csv、剪映草稿包/、Premiere工程（均自包含）。")
                log("   注意：清理后「重配此段」与「重新导出草稿/工程」不可用，如需请先导出。")
            else:
                log("没有可清理的中间产物（master_chunks / 成片_segments 均不存在）。")
            with JOB.lock:
                JOB.ok = True
                JOB.result = None  # 分镜段已删，作废内存结果 → 后续导出走磁盘回读并给出友好提示
        elif action == "probe":
            # 环境自检时对 Edge TTS 走 live_check=True（用户显式点了自检就跑一次短合成，
            # 明确"能真发出请求且获得音频"）；其他引擎沿用现有 probe。
            if studio_settings.cloud_only():
                _engines = (DotsRemoteEngine().probe(), EdgeTtsEngine().probe(live_check=True))
            else:
                _engines = (MockEngine().probe(), DotsLocalEngine().probe(),
                             DotsRemoteEngine().probe(), FishLocalEngine().probe(),
                             EdgeTtsEngine().probe(live_check=True))
            for status in _engines:
                log(("✅ " if status.available else "⛔ ") + f"{status.key}：{status.detail}")
            aligner = WhisperAligner().probe()
            log(("✅ " if aligner.available else "⛔ ") + f"whisper：{aligner.detail}")
            with JOB.lock:
                JOB.ok = True
        elif action == "component":
            key = str(payload.get("component_key") or "")
            toolbox.install_component(key, log)
            with JOB.lock:
                JOB.ok = True
        elif action == "voice_try":
            # 音色试听：按 音色+引擎+参数+试听句 缓存到本地；命中缓存直接复用，不重复渲染。
            import hashlib

            sample = str(payload.get("text") or "水星配音对齐，整篇克隆，逐行对齐，一句一画面。")
            engine_key = str(payload.get("engine") or "mock")
            # P0-3：严格保留 0；由 SynthesisOptions/_validate_options 上层拒错
            opts = SynthesisOptions(
                num_steps=int(_as_number(payload.get("num_steps"), 10)),
                guidance_scale=float(_as_number(payload.get("guidance_scale"), 1.2)),
                speed=float(_as_number(payload.get("speed"), 1.0)),
                max_pause_seconds=float(_as_number(payload.get("max_pause_seconds"), 0.0)),
                seed=int(_as_number(payload.get("seed"), 42)),
                edge_voice=str(payload.get("edge_voice") or ""),
                edge_pitch=int(_as_number(payload.get("edge_pitch"), 0)),
                edge_style=str(payload.get("edge_style") or "general"),
            )
            # Edge 试听按预设声线区分缓存；其他引擎按用户音色 id
            if engine_key == "edge_tts":
                voice_id = str(payload.get("edge_voice") or "").strip() or "默认预设"
                try_voice = None    # Edge 不看 VoiceRef
            else:
                voice_id = str(payload.get("voice_id") or "默认声线")
                try_voice = voice   # 沿用外层已解析的 voice（可能为 None）
            sig = hashlib.md5(  # noqa: S324 —— 仅做缓存键，非安全用途
                f"{voice_id}|{engine_key}|{opts.to_payload()}|{sample}".encode("utf-8")
            ).hexdigest()[:10]
            cache_dir = studio_settings.clones_dir() / "试听缓存"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{_safe_name(voice_id)}_{engine_key}_{sig}.wav"
            if cached.is_file() and cached.stat().st_size > 44:
                # 缓存命中**绝不**刷云 GPU——纯磁盘复用不打远程
                log(f"✅ 命中试听缓存，直接复用（未重复渲染）：{cached.name}")
                with JOB.lock:
                    JOB.ok = True
                    JOB.result = {"try_audio": str(cached)}
            else:
                # 缓存未命中：候选 .staging.wav → 后处理 → 原子 replace 到 cached
                # P0-4：保活钩子**紧邻** engine.synthesize_full；after 放 finally；
                # 参数校验、缓存查找已在前面完成，钩子不会因这些不真发远程的情况误刷。
                log(f"用引擎 {engine_key} 合成试听句（{len(sample)} 字）…首次合成后会缓存，之后试听秒开。")
                _engine = pipeline.make_engine(engine_key)
                _uses_remote_gpu = pipeline.is_cloud_gpu_engine(engine_key)
                # P0-3：透过 SynthesisOptions 的合法性校验（sanitize speed/max_pause）
                from .engines.longform import _apply_postprocess, _validate_options
                _validate_options(opts)

                cand = cached.with_suffix(cached.suffix + ".staging.wav")
                cand_meta = cand.with_suffix(".json")
                for p in (cand, cand_meta):
                    try:
                        if p.exists():
                            p.unlink()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    if _uses_remote_gpu:
                        mark_cloud_gpu_active_now()      # before：紧邻真实调用
                    try:
                        _engine.synthesize_full(sample, try_voice, cand, opts)
                    finally:
                        if _uses_remote_gpu:
                            mark_cloud_gpu_active_now()  # after：finally，成功/失败都刷
                    actual = _apply_postprocess(cand, opts, _engine, log=log)
                    if not cand.is_file() or cand.stat().st_size < 44:
                        raise RuntimeError(f"试听候选文件缺失或过小：{cand}")
                    import os as _os
                    _os.replace(str(cand), str(cached))
                finally:
                    for p in (cand, cand_meta):
                        try:
                            if p.exists():
                                p.unlink()
                        except Exception:  # noqa: BLE001
                            pass
                with JOB.lock:
                    JOB.ok = True
                    JOB.result = {"try_audio": str(cached)}
                log(f"✅ 试听已生成并缓存：{cached.name}（{actual:.2f}s）")
        elif action == "fish_server":
            toolbox.start_fish_server(log)
            with JOB.lock:
                JOB.ok = True
        elif action == "fish_server_stop":
            toolbox.stop_fish_server(log)
            with JOB.lock:
                JOB.ok = True
        elif action == "font":
            key = str(payload.get("font_key") or "")
            font_library.install_font(key, log)
            with JOB.lock:
                JOB.ok = True
        elif action == "envcheck":
            from pathlib import Path as _P

            from .version import full_version

            log(f"版本：{full_version()}")
            root = studio_settings.data_root()
            log(f"总目录：{root}")
            log(f"设置文件：{studio_settings.SETTINGS_FILE}")
            for label, path in (("组件", studio_settings.components_root()),
                                ("音色库", voice_library.voices_root(_vroot())),
                                ("克隆音频", root / studio_settings.DIR_CLONES),
                                ("字体", root / studio_settings.DIR_FONTS)):
                log(f"  {label}：{path}（{'存在' if path.is_dir() else '将在首次使用时创建'}）")
            ff = _P(studio_settings.ffmpeg_tool("ffmpeg")).is_file() and _P(studio_settings.ffmpeg_tool("ffprobe")).is_file()
            log(("✅ " if ff else "⛔ ") + "ffmpeg / ffprobe" + ("" if ff else "：未找到，请放到软件目录旁或加入 PATH"))
            for c in toolbox.component_statuses():
                log(("✅ " if c["installed"] else "⛔ ") + f"{c['name']}：{c['detail']}")
            # whisper-cli 缺失时的自动诊断：列出 whisper.cpp 目录真实内容，一图定位
            if not studio_settings.whisper_cli_path():
                home = studio_settings.whisper_home()
                if home.is_dir():
                    log(f"🔎 whisper 目录诊断（{home}）：")
                    for child in sorted(home.iterdir())[:12]:
                        log(f"    {child.name}{'/' if child.is_dir() else ''}")
                        if child.is_dir():
                            for sub in sorted(child.iterdir())[:8]:
                                log(f"        {sub.name}")
                else:
                    log(f"🔎 whisper 目录尚不存在：{home}（点组件行「下载」自动创建）")
            installed_fonts = font_library.list_fonts()
            log(f"字体库：{len(installed_fonts)} 款可用" + ("（" + "、".join(f['name'] for f in installed_fonts[:6]) + "…）" if installed_fonts else "（可在下方下载或把 ttf/otf 放入字体目录）"))
            voices = voice_library.list_voices(_vroot())
            log(f"音色库：{len(voices)} 个音色" + ("（" + "、".join(v.name for v in voices[:6]) + "）" if voices else "（未登记时使用默认声线）"))
            log("✅ 环境自检完成：已存在的组件/模型不会重复下载；整个总目录可拷贝到其他电脑直接使用。")
            with JOB.lock:
                JOB.ok = True
        elif action == "verify":
            from . import cli

            code = cli.verify()
            log("✅ 端到端自检通过" if code == 0 else f"❌ 自检未通过（退出码 {code}）")
            with JOB.lock:
                JOB.ok = code == 0
        else:
            raise ValueError(f"未知动作：{action}")
    except Exception as exc:
        JOB.append("❌ 失败：" + "".join(traceback.format_exception_only(exc)).strip())
    finally:
        # **finally 里绝不打"成片中不包含"汇总**——render 失败 / voice_try / probe 等
        # 根本没成片的 action 会误报。汇总由各成片成功分支自行调用 `_log_mix_summary_if_needed`。
        with JOB.lock:
            JOB.running = False
            JOB.done = True
        # **finally 里绝不无条件刷云 GPU**：真远程动作各自在成功完成后显式 mark。


# ------------------------------------------------------------------ HTTP
from . import licensing as _licensing_pkg


# 允许在未激活状态下访问的**白名单前缀**：
#   * 首页 HTML / 静态 JS/CSS —— 让登录页能加载
#   * /api/license/* —— 激活流本身
#   * favicon / 组件静态 —— 页面基础资源
# 其他所有 /api/* 都强制经 license gate。
_LICENSE_PUBLIC_ROUTES = frozenset({
    "/", "/index.html", "/favicon.ico",
    "/api/license/status", "/api/license/activate",
    "/api/license/logout", "/api/license/deactivate",
})


def _license_gate_check(route: str) -> bool:
    """路由是否允许在**未激活**状态下访问。True = 放行；False = 拦截并返回 403。"""
    if route in _LICENSE_PUBLIC_ROUTES:
        return True
    if route.startswith("/api/license/"):
        return True
    # 主界面的非 API 静态资源（web/ 下）也放行，让登录页能用
    if not route.startswith("/api/"):
        return True
    return False


def _license_gate_enabled() -> bool:
    """License gate 是否启用。

    优先级（新，堵 env 绕过路径）：
      1) 打包时 _build_info.PACKAGED == True  → **强制启用**，忽略任何 env
         （攻击者删 vbs、写 .bat 跳过 env 已经不能关掉 gate）
      2) 未打包（开发/测试）：默认 **不强制**；
         env DUB_ALIGN_LICENSE_REQUIRED=1 显式启用；
         env DUB_ALIGN_LICENSE_DISABLE=1 显式关掉。
    """
    try:
        from . import _build_info as _bi
        if getattr(_bi, "PACKAGED", False):
            return True
    except ImportError:
        pass
    disable = os.environ.get("DUB_ALIGN_LICENSE_DISABLE", "").strip().lower()
    if disable in ("1", "true", "yes", "on"):
        return False
    enable = os.environ.get("DUB_ALIGN_LICENSE_REQUIRED", "").strip().lower()
    if enable in ("1", "true", "yes", "on"):
        return True
    return False


def _license_gate_active() -> bool:
    try:
        return _licensing_pkg.get_manager().is_active()
    except Exception:  # noqa: BLE001
        # licensing 子系统本身崩了 → **fail closed**，不允许通过
        return False


def _license_should_block(route: str) -> bool:
    """真正的 gate 决策：既要 gate 启用，又要路由非公开，且当前未激活。"""
    if not _license_gate_enabled():
        return False
    if _license_gate_check(route):
        return False
    return not _license_gate_active()


class _Handler(BaseHTTPRequestHandler):
    # R12-9：显式 HTTP/1.1——CSV chunked / Range 206 都需要 1.1
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------------- License
    def _license_state_json(self) -> dict:
        """公开状态：不返回激活码明文，只返回够 UI 展示的字段。"""
        try:
            st = _licensing_pkg.get_manager().state()
        except Exception as exc:  # noqa: BLE001
            return {
                "activated": False, "active": False, "last_error": str(exc)[:200],
            }
        gate_enabled = _license_gate_enabled()
        # UI 判 `active`：
        #  - gate 未启用（开发/测试）→ 恒 true，UI 直接进主界面
        #  - gate 启用 → 走 licensing 子系统真实判定
        ui_active = True if not gate_enabled else _license_gate_active()
        return {
            "activated": st.activated,
            "active": ui_active,
            "gate_enabled": gate_enabled,
            "expire_at": st.expire_at,
            "server_time": st.server_time,
            "heartbeat_interval": st.heartbeat_interval,
            "last_action": st.last_action,
            "last_error": st.last_error,
            # 已激活才返回设备名 / server；未激活不给（避免识别 + 定位服务端）
            "device_name": (
                _licensing_pkg.get_manager()._device if ui_active else ""
            ),
            "app_version": _licensing_pkg.APP_VERSION,
            # app_id 和 server 完全不再从 API 返回（防止逆向者用来伪造激活服务端）
            # 前端只用 device_name + app_version 展示，够用。
            # 只返回激活码前后 4 位便于用户确认，不泄露全码
            "code_hint": (
                (st.code[:4] + "…" + st.code[-4:])
                if len(st.code) >= 10 else ("已设置" if st.code else "")
            ),
        }

    def _handle_license_activate(self, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8", "ignore")) if body else {}
        except json.JSONDecodeError:
            payload = {}
        code = str(payload.get("code") or "").strip()
        if not code:
            self._json({"error": "请填写激活码"}, 400)
            return
        try:
            mgr = _licensing_pkg.get_manager()
            r = mgr.activate(code)
        except _licensing_pkg.LicenseDenied as exc:
            self._json({"error": str(exc),
                         "detail": exc.detail or str(exc),
                         "status": exc.http_status}, 403)
            return
        except _licensing_pkg.NetworkError as exc:
            self._json({"error": str(exc)}, 502)
            return
        except _licensing_pkg.LicensingError as exc:
            self._json({"error": str(exc),
                         "status": getattr(exc, "http_status", 500)}, 500)
            return
        self._json({
            "success": True, "message": r.message,
            "expire_at": r.expire_at,
            "state": self._license_state_json(),
        })

    def _handle_license_logout(self) -> None:
        try:
            _licensing_pkg.get_manager().logout()
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)
            return
        self._json({"success": True, "state": self._license_state_json()})

    def _handle_license_deactivate(self) -> None:
        """完全清本地激活状态（回到未激活）。"""
        try:
            _licensing_pkg.get_manager().deactivate()
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)
            return
        self._json({"success": True, "state": self._license_state_json()})

    def do_GET(self) -> None:
        route = urlparse(self.path).path
        # ==================== License gate ====================
        if route == "/api/license/status":
            self._json(self._license_state_json())
            return
        if _license_should_block(route):
            st = self._license_state_json()
            self._json({
                "error": "license_required",
                "message": "请先输入激活码激活软件。",
                "state": st,
            }, 403)
            return
        # ======================================================
        if route in {"/", "/index.html"}:
            body = _INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/api/state":
            self._json(_state_payload())
            return
        if route == "/api/job":
            self._json(JOB.snapshot())
            return
        if route == "/api/jobs":
            with TASKS_LOCK:
                jobs = [t.snapshot() for t in TASKS.values()]
            self._json({"jobs": jobs})
            return
        if route == "/api/queue":                       # ⑨ 生成任务队列状态
            self._json(_gen_queue_snapshot())
            return
        if route == "/api/edit_queue":                  # ⑧ 待编辑队列（顺带清过期）
            items = edit_queue.list_active()
            for it in items:                            # 重启后重新放行各成片所在目录，否则预览/试听 403
                _allow_media_root(str(it.get("output_dir") or ""))
            self._json({"items": items})
            return
        if route == "/api/audio":
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self._serve_media(Path(str(query.get("path") or "")))
            return
        if route == "/api/clones":
            self._json({"clones": _clones_payload()})
            return
        if route == "/api/chunks":
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self._json(_chunks_payload(str(query.get("output_dir") or "")))
            return
        if route == "/api/assets":
            self._json({"assets": _assets_payload(),
                        "assets_dir": str(studio_settings.audio_assets_dir())})
            return
        if route == "/api/asset":
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self._serve_media(studio_settings.audio_assets_dir() / Path(query.get("file") or "").name,
                              skip_root_check=True)
            return
        if route == "/api/config":
            self._json({"presets": _load_config_presets()})
            return
        if route.startswith("/api/voices/") and route.endswith("/audio"):
            voice_id = unquote(route[len("/api/voices/"):-len("/audio")])
            try:
                entry = voice_library.get_voice(_vroot(), voice_id)
            except KeyError as exc:
                self._json({"error": str(exc)}, 404)
                return
            self._serve_media(entry.reference_wav, skip_root_check=True)
            return
        if route == "/api/browse":
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            self._json(_browse(query.get("path") or "", query.get("files") or ""))
            return
        if route == "/api/settings":
            root = studio_settings.data_root()
            remote_ep, remote_key = studio_settings.dots_remote_config()
            self._json({"data_root": str(root),
                        "default_data_root": str(studio_settings.default_data_root()),
                        "component_root": str(studio_settings.component_root()),
                        # 云配音（远程 GPU）配置：地址明示；Key 仅回「是否已设置」，不回明文
                        "dots_remote_endpoint": remote_ep,
                        "dots_remote_api_key_set": bool(remote_key),
                        # Edge TTS（免费云端预设音色）Worker 服务根地址；空=未配置
                        "edge_tts_endpoint": edge_tts_mod.edge_tts_endpoint(),
                        "paths": {  # 各类下载/资产的实际落地目录（界面明示，杜绝「下到哪了」的疑问）
                            "组件": str(studio_settings.components_root()),
                            "whisper 模型": str(studio_settings.whisper_models_dir()),
                            "字体": str(root / studio_settings.DIR_FONTS),
                            "克隆音频": str(root / studio_settings.DIR_CLONES),
                            "音色库": str(root / studio_settings.DIR_VOICES),
                        }})
            return
        if route == "/api/fonts":
            self._json({"installed": font_library.list_fonts(),
                        "pack": font_library.font_statuses(),
                        "fonts_dir": str(studio_settings.fonts_dir())})
            return
        if route.startswith("/fonts/"):
            name = unquote(route.rsplit("/", 1)[-1])
            file = studio_settings.fonts_dir() / Path(name).name
            if file.is_file() and file.suffix.lower() in font_library.FONT_SUFFIXES:
                body = file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/ttf")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": "字体不存在"}, 404)
            return
        if route == "/api/voices/export":
            payload = voice_library.export_voices_zip(_vroot())
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=voices.zip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if route == "/api/components":
            self._json({"components": toolbox.component_statuses()})
            return
        if route == "/api/path_exists":
            # 前端「代表样本」等文件输入实时校验用；只返存在性 + is_file
            # **不回显路径**（防止未激活/日志中泄露）；不含目录内容
            try:
                p = params.get("p", [""])[0] if isinstance(params, dict) else ""
            except Exception:  # noqa: BLE001
                p = ""
            exists = False
            is_file = False
            try:
                if p:
                    _pp = os.path.abspath(p)
                    exists = os.path.exists(_pp)
                    is_file = os.path.isfile(_pp)
            except Exception:  # noqa: BLE001
                pass
            self._json({"exists": bool(exists), "is_file": bool(is_file)})
            return
        if route == "/api/probe":
            # /api/probe 只做**轻量**探活：Edge TTS 走配置检查（live_check=False），
            # 避免"探测组件"按钮点一次就无故拉一次外网请求。真实连通用「测试连接」按钮。
            edge_probe = EdgeTtsEngine().probe(live_check=False)
            _probe_pairs = (((DotsRemoteEngine().probe(), "dots.tts 云端"),
                              (edge_probe, "Edge TTS 免费云端"))
                            if studio_settings.cloud_only() else (
                                (MockEngine().probe(), "mock 引擎"),
                                (DotsLocalEngine().probe(), "dots.tts"),
                                (DotsRemoteEngine().probe(), "dots.tts 云端"),
                                (FishLocalEngine().probe(), "fish-speech"),
                                (edge_probe, "Edge TTS 免费云端"),
                            ))
            statuses = [
                {"key": s.key, "name": n, "available": s.available, "detail": s.detail}
                for s, n in _probe_pairs
            ]
            aligner = WhisperAligner().probe()
            statuses.append({"key": aligner.key, "name": "whisper 尺子",
                             "available": aligner.available, "detail": aligner.detail})
            from pathlib import Path as _P

            ff = _P(studio_settings.ffmpeg_tool("ffmpeg")).is_file() and _P(studio_settings.ffmpeg_tool("ffprobe")).is_file()
            statuses.insert(0, {"key": "ffmpeg", "name": "ffmpeg / ffprobe",
                                "available": ff,
                                "detail": "渲染就绪。" if ff else "未找到 ffmpeg/ffprobe，无法渲染成片。"})
            self._json({"components": statuses})
            return
        if route == "/api/cloud/status":
            from . import cloud_gpu
            self._json(cloud_gpu.manager().status())
            return
        # Excel 批量带货配音（独立页面 + 独立 API 前缀）
        if route == "/bulk_dub" or route == "/bulk_dub.html":
            page = Path(__file__).parent / "web" / "bulk_dub.html"
            if page.is_file():
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        # 音频白名单下发：只允许 data_root/批量带货/试听 与 输出目录下的文件
        # R12-10：媒体访问改为按 task_id / probe token 授权——
        # **不再**允许客户端提交任意文件绝对路径
        if route == "/api/bulk_dub/task_output":
            from .bulk_dub import service as bulk_service
            from .bulk_dub.store import is_safe_id as _is_safe

            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            task_id = query.get("task_id") or ""
            if not _is_safe(task_id):
                self._json({"error": "非法 task_id"}, 400)
                return
            svc = bulk_service.get_service()
            row = svc.store.get(task_id)
            if row is None:
                self._json({"error": "task 不存在"}, 404)
                return
            if not row.output_path or not Path(row.output_path).is_file():
                self._json({"error": "该任务尚无成片文件"}, 404)
                return
            # 历史批次也允许访问——只用 task_id 的 output_path 白名单
            self._serve_bulk_audio(row.output_path,
                                    [Path(row.output_path).resolve().parent])
            return
        if route == "/api/bulk_dub/probe_audio":
            from .bulk_dub import service as bulk_service
            from .bulk_dub.service import DEFAULT_VOICE_ID, DEFAULT_SPEED, VOICE_IDS

            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            voice_id = query.get("voice_id") or DEFAULT_VOICE_ID
            try:
                speed = float(query.get("speed") or DEFAULT_SPEED)
            except ValueError:
                self._json({"error": "非法 speed"}, 400)
                return
            if voice_id not in VOICE_IDS or not (0.5 <= speed <= 2.0):
                self._json({"error": "非法 voice_id/speed"}, 400)
                return
            probe_root = (studio_settings.data_root() / "批量带货" / "试听").resolve()
            wav = probe_root / f"{voice_id}_{speed:.2f}.wav"
            if not wav.is_file():
                self._json({"error": "试听文件不存在，请先 POST /try_voice"}, 404)
                return
            self._serve_bulk_audio(str(wav), [probe_root])
            return
        if route.startswith("/api/bulk_dub"):
            from .bulk_dub import api as bulk_api

            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            # R11-8：CSV 走真流式（不整份进内存）
            if route == "/api/bulk_dub/csv":
                self._serve_bulk_csv_streaming(query)
                return
            try:
                handled, status, body, ctype = bulk_api.dispatch_get(route, query)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"bulk_dub GET 异常：{exc}"}, 500)
                return
            if handled:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self._json({"error": "not found"}, 404)

    def _serve_bulk_csv_streaming(self, query: dict) -> None:
        """R11-8：万级 CSV **真流式**——用 chunked transfer encoding，
        每次 iter_csv_chunks 产出一批就发一批，永不把全表加载进内存。

        R13-P0-4：在发 200 headers **之前**先做 batch_id 存在性校验——避免
        \"未知 batch 返回 200 空 CSV\" 的假成功。
        """
        from .bulk_dub import csv_export as _ce
        from .bulk_dub.service import get_service
        from .bulk_dub.store import is_safe_id as _is_safe

        batch_id = query.get("batch_id") or None
        if batch_id and not _is_safe(batch_id):
            self._json({"error": "非法 batch_id"}, 400)
            return
        svc = get_service()
        if batch_id and not svc.store.batch_exists(batch_id):
            self._json({"error": f"batch 不存在：{batch_id}"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                          "attachment; filename=bulk_dub_result.csv")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in _ce.iter_csv_chunks(svc.store.iter_all(batch_id=batch_id)):
                self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_bulk_audio(self, path_str: str,
                           allowed_roots: list) -> None:
        """把批量配音的 wav 试听/成片安全下发到浏览器。

        R11-8：**试听（WAV/MP3）**分块流式；成片 MP4 支持 HTTP Range（断点/拖动）
        并按 64KB 分块发；绝不 read_bytes 整份加载。
        """
        if not path_str:
            self._json({"error": "缺少 path"}, 400)
            return
        try:
            candidate = Path(path_str).resolve()
        except OSError:
            self._json({"error": "路径非法"}, 400)
            return
        if not candidate.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        ok = False
        for root in allowed_roots:
            try:
                candidate.relative_to(root)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            self._json({"error": "路径不在允许范围"}, 403)
            return
        ext = candidate.suffix.lower().lstrip(".")
        # 试听只允许 WAV/MP3；MP4 走 Range 播放
        allowed_exts = {"wav", "mp3", "mp4", "m4a"}
        if ext not in allowed_exts:
            self._json({"error": f"不支持的媒体后缀：{ext}"}, 400)
            return
        ctype = {"wav": "audio/wav", "mp3": "audio/mpeg",
                  "m4a": "audio/mp4", "mp4": "video/mp4"}.get(ext, "application/octet-stream")
        file_size = candidate.stat().st_size
        # R12-10 解析 Range —— 支持 bytes=N-M / bytes=N- / bytes=-N（后缀 N 字节）
        range_hdr = (self.headers.get("Range") or "").strip()
        start, end = 0, file_size - 1
        status = 200
        if range_hdr:
            if not range_hdr.lower().startswith("bytes="):
                # 明确 416
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            spec = range_hdr[6:].strip()
            if "," in spec:
                # 多 Range 明确不支持
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                if "-" not in spec:
                    raise ValueError("no dash")
                a, b = spec.split("-", 1)
                a, b = a.strip(), b.strip()
                if not a and not b:
                    raise ValueError("empty range")
                if not a:  # 后缀：最后 N 字节
                    suffix = int(b)
                    if suffix <= 0:
                        raise ValueError("bad suffix")
                    if suffix >= file_size:
                        start, end = 0, file_size - 1
                    else:
                        start = file_size - suffix
                        end = file_size - 1
                elif not b:  # 开放结尾
                    start = int(a)
                    end = file_size - 1
                else:
                    start = int(a)
                    end = int(b)
                if start < 0 or end < 0 or start > end or start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, file_size - 1)
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # 64KB 分块流式发送
        remaining = length
        try:
            with open(candidate, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _handle_open_output_dir(self, parsed) -> None:
        """R12-8：只允许打开某 batch_id 的 output_dir（已入库）——白名单守卫。"""
        import subprocess as _sp
        import sys as _sys
        from .bulk_dub import service as bulk_service
        from .bulk_dub.store import is_safe_id as _is_safe

        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        batch_id = query.get("batch_id") or ""
        if not _is_safe(batch_id):
            self._json({"error": "非法 batch_id"}, 400)
            return
        svc = bulk_service.get_service()
        b = svc.store.get_batch(batch_id)
        if not b:
            self._json({"error": "batch 不存在"}, 404)
            return
        out_dir = b.get("output_dir") or ""
        if not out_dir or not Path(out_dir).is_dir():
            self._json({"error": f"output_dir 不存在：{out_dir}"}, 404)
            return
        try:
            if _sys.platform.startswith("win"):
                _sp.Popen(["explorer", str(out_dir)])
            elif _sys.platform == "darwin":
                _sp.Popen(["open", str(out_dir)])
            else:
                _sp.Popen(["xdg-open", str(out_dir)])
        except (OSError, FileNotFoundError) as exc:
            self._json({"error": f"无法打开：{exc}"}, 500)
            return
        self._json({"opened": out_dir})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path

        # ==================== License 路由 & gate ====================
        if route == "/api/license/activate":
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length > 0 else b""
            self._handle_license_activate(body)
            return
        if route == "/api/license/logout":
            self._handle_license_logout()
            return
        if route == "/api/license/deactivate":
            self._handle_license_deactivate()
            return
        # 其他 POST 都强制 gate
        if _license_should_block(route):
            self._json({
                "error": "license_required",
                "message": "请先输入激活码激活软件。",
                "state": self._license_state_json(),
            }, 403)
            return
        # ============================================================

        # R12-8：真"在文件管理器中打开"——只允许打开当前存在的 batch 输出目录
        if route == "/api/bulk_dub/open_output_dir":
            self._handle_open_output_dir(parsed)
            return
        if route.startswith("/api/bulk_dub"):
            from .bulk_dub import api as bulk_api

            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            # R11-11：Content-Length 校验
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                # 缺失 → 411 Length Required（不接受 chunked upload）
                self._json({"error": "缺少 Content-Length"}, 411)
                return
            try:
                length = int(raw_len)
            except ValueError:
                self._json({"error": f"非法 Content-Length：{raw_len}"}, 400)
                return
            if length < 0:
                self._json({"error": f"Content-Length 不能为负：{length}"}, 400)
                return
            max_body = 20 * 1024 * 1024 if route in ("/api/bulk_dub/preview", "/api/bulk_dub/start") else 256 * 1024
            if length > max_body:
                self._json({"error": f"上传体积过大：Content-Length={length} > {max_body}"}, 413)
                return
            body = self.rfile.read(length) if length > 0 else b""
            ctype = self.headers.get("Content-Type") or ""
            try:
                handled, status, out_body, out_ctype = bulk_api.dispatch_post(
                    route, query, body, ctype,
                )
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"bulk_dub POST 异常：{exc}"}, 500)
                return
            if handled:
                self.send_response(status)
                self.send_header("Content-Type", out_ctype)
                self.send_header("Content-Length", str(len(out_body)))
                self.end_headers()
                self.wfile.write(out_body)
                return
        if route == "/api/voices":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_UPLOAD:
                self._json({"error": f"音频大小非法：{length}"}, 400)
                return
            suffix = Path(query.get("filename") or "参考.wav").suffix or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    remaining -= len(chunk)
                temp = Path(handle.name)
            try:
                params = {}
                for key in ("num_steps", "guidance_scale", "speed", "max_pause_seconds", "seed"):
                    if key in query:
                        params[key] = query[key]
                entry = voice_library.register_voice(
                    _vroot(), query.get("name") or "未命名",
                    temp, transcript=query.get("transcript") or "",
                    note=query.get("note") or "",
                    engine=query.get("engine") or voice_library.DEFAULT_ENGINE,
                    params=params or None)
                self._json({"ok": True, "voice_id": entry.voice_id})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            finally:
                temp.unlink(missing_ok=True)
            return
        if route == "/api/settings":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                out = {"ok": True}
                raw = str(payload.get("data_root") or payload.get("component_root") or "")
                if raw.strip():
                    out["data_root"] = str(studio_settings.set_data_root(raw))
                # 云配音（远程 GPU）：地址/Key。空字符串=清空该项；未提供该键=保持不变
                remote_update = {}
                if "dots_remote_endpoint" in payload:
                    remote_update["dots_remote_endpoint"] = str(payload.get("dots_remote_endpoint") or "").strip().rstrip("/")
                if "dots_remote_api_key" in payload:
                    remote_update["dots_remote_api_key"] = str(payload.get("dots_remote_api_key") or "").strip()
                # Edge TTS Worker 服务根地址（默认空；空字符串=显式清空）
                if "edge_tts_endpoint" in payload:
                    raw_ep = str(payload.get("edge_tts_endpoint") or "").strip()
                    # 复用引擎里的归一化逻辑，去掉误填的 /v1/audio/speech 尾巴
                    remote_update["edge_tts_endpoint"] = edge_tts_mod._normalize_endpoint_root(raw_ep) if raw_ep else ""
                if remote_update:
                    # save_settings 会跳过 None；空串是有效值（清空），故直接写
                    merged = studio_settings.load_settings()
                    merged.update(remote_update)
                    studio_settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
                    studio_settings.SETTINGS_FILE.write_text(
                        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
                    ep, key = studio_settings.dots_remote_config()
                    out["dots_remote_endpoint"] = ep
                    out["dots_remote_api_key_set"] = bool(key)
                # 回显 Edge TTS 归一化后的 endpoint（前端可据此刷新展示）
                out["edge_tts_endpoint"] = edge_tts_mod.edge_tts_endpoint()
                self._json(out)
            except Exception as exc:
                self._json({"error": f"保存失败：{exc}"}, 400)
            return
        if route.startswith("/api/cloud/") and route != "/api/cloud/status":
            from . import cloud_gpu
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
            except Exception as exc:
                self._json({"error": f"参数解析失败：{exc}"}, 400)
                return
            try:
                if route == "/api/cloud/save":
                    cloud_gpu.save_cloud_config(payload)
                    self._json(cloud_gpu.manager().status())
                    return
                if route == "/api/cloud/wake":
                    self._json(cloud_gpu.manager().wake())
                    return
                if route == "/api/cloud/sleep":
                    self._json(cloud_gpu.manager().sleep(reason="manual"))
                    return
                if route == "/api/cloud/pause_auto":
                    minutes = float(payload.get("minutes") or 0.0)
                    self._json(cloud_gpu.manager().pause_auto(minutes))
                    return
                if route == "/api/cloud/start_api":
                    self._json(cloud_gpu.manager().start_via_api())
                    return
            except Exception as exc:
                self._json({"error": f"云 GPU 操作失败：{exc}"}, 500)
                return
            self._json({"error": "not found"}, 404)
            return
        if route == "/api/fonts":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if 0 < length <= _MAX_UPLOAD else b""
            try:
                saved = font_library.save_uploaded_font(query.get("filename") or "字体.ttf", data)
                self._json({"ok": True, "saved": saved})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if route == "/api/edge_tts/test":
            # 「测试连接」按钮的显式入口：这时才允许 Edge TTS 发一次真实合成请求验证
            # 服务可达；结果直接以 EngineStatus 形式回传（key/available/detail）。
            status = EdgeTtsEngine().probe(live_check=True)
            self._json({"key": status.key, "available": status.available, "detail": status.detail})
            return
        if route == "/api/assets":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > _MAX_UPLOAD:
                self._json({"error": f"文件大小非法：{length}"}, 400)
                return
            name = Path(query.get("filename") or "素材.mp3").name
            if Path(name).suffix.lower() not in _MEDIA_SUFFIXES:
                self._json({"error": f"不支持的音频格式：{Path(name).suffix}"}, 400)
                return
            target = studio_settings.audio_assets_dir() / name
            try:
                with target.open("wb") as handle:
                    remaining = length
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        handle.write(chunk)
                        remaining -= len(chunk)
                self._json({"ok": True, "file": target.name})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if route == "/api/config":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                name = str(payload.get("name") or "").strip()
                if not name:
                    raise ValueError("配置名不能为空。")
                _save_config_preset(name, payload.get("config") or {})
                self._json({"ok": True, "presets": _load_config_presets()})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if route == "/api/script/parse":
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if 0 < length <= _MAX_UPLOAD else b""
            kind = (query.get("kind") or "").lower()
            # 本机路径导入（导入表格/TXT 走服务端选文件）：直接按真实路径读取，
            # 并回带 folder 供前端把「分镜目录/输出目录」默认设为文档所在文件夹。
            src_path = (query.get("path") or "").strip()
            folder = ""
            try:
                if src_path:
                    p = Path(src_path)
                    if not p.is_file():
                        self._json({"error": f"文件不存在：{p}"}, 400)
                        return
                    if p.stat().st_size > _MAX_UPLOAD:
                        self._json({"error": f"文件过大（>{_MAX_UPLOAD // (1024*1024)}MB）：{p.name}"}, 400)
                        return
                    data = p.read_bytes()   # 读盘放进 try：权限/损坏也返回 JSON 而非 500 断连
                    folder = str(p.parent)
                if kind == "xlsx":
                    lines = xlsx_reader.read_column(data, query.get("column") or "B")
                elif kind == "txt":
                    text = data.decode("utf-8-sig", errors="replace")
                    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
                else:
                    raise ValueError("kind 应为 xlsx 或 txt。")
                if str(query.get("header") or "") in ("1", "true") and lines:
                    lines = lines[1:]  # 跳过表头行（如「口播文稿内容」这类列标题）
                if not lines:
                    raise ValueError("没有读到任何文案行（xlsx 请确认列号；txt 请确认一行一句）。")
                self._json({"ok": True, "lines": lines, "folder": folder})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if route == "/api/voices/import":
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length) if 0 < length <= _MAX_UPLOAD else b""
            try:
                imported = voice_library.import_voices_zip(_vroot(), data)
                self._json({"ok": True, "imported": imported})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        if route == "/api/run":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "JSON 无效"}, 400)
                return
            action = str(payload.get("action") or "")
            if action == "component_stop":
                # 立即掐断卡住的安装子进程（不进任务队列，不被占用的槽阻塞）
                key = str(payload.get("component_key") or "")
                logs: list[str] = []
                toolbox.stop_component(key, logs.append)
                self._json({"ok": True, "log": logs})
                return
            for key in ("output_dir", "shots_dir"):
                _allow_media_root(str(payload.get(key) or ""))
            job, busy = _start_task(action, payload)
            if job is None:
                self._json({"error": busy}, 409)
                return
            self._json({"ok": True, "slot": job.slot})
            return
        if route == "/api/queue":                       # ⑨ 生成任务队列
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "JSON 无效"}, 400)
                return
            act = str(payload.get("action") or "add")
            if act == "remove":
                self._json({"ok": _gen_queue_remove(str(payload.get("id") or ""))})
                return
            if act == "retry":
                self._json({"ok": _gen_queue_retry(str(payload.get("id") or ""))})
                return
            if act == "cancel":
                self._json({"ok": _gen_queue_cancel(str(payload.get("id") or ""))})
                return
            if act in ("pause", "resume"):
                _gen_queue_set_paused(act == "pause")
                self._json({"ok": True, "paused": act == "pause"})
                return
            if act == "clear":
                self._json({"ok": True, "cleared": _gen_queue_clear_finished()})
                return
            # add：把整份配置冻结成一个后台克隆任务
            for key in ("output_dir", "shots_dir"):
                _allow_media_root(str(payload.get(key) or ""))
            if not str(payload.get("output_dir") or "").strip():
                self._json({"error": "请先选择「输出目录」再加入队列。"}, 400)
                return
            if not str(payload.get("text") or "").strip():
                self._json({"error": "请先填写「待合成文案」再加入队列。"}, 400)
                return
            title = str(payload.get("title") or Path(str(payload["output_dir"])).name or "成片")
            task_id = _gen_enqueue(payload, title)
            self._json({"ok": True, "id": task_id, "title": title})
            return
        if route == "/api/edit_queue":                  # ⑧ 待编辑队列 操作
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"error": "JSON 无效"}, 400)
                return
            act = str(payload.get("action") or "")
            item_id = str(payload.get("id") or "")
            if act == "remove":
                self._json({"ok": edit_queue.remove(item_id)})
            elif act == "touch":
                self._json({"ok": edit_queue.touch(item_id)})
            else:
                self._json({"error": f"未知操作：{act}"}, 400)
            return
        if route == "/api/open_folder":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                target = Path(str(payload.get("path") or "")).expanduser()
                if not target.is_dir():
                    raise FileNotFoundError(f"目录不存在：{target}")
                _open_folder(target)
                self._json({"ok": True})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
        self._json({"error": "not found"}, 404)

    def _serve_media(self, file: Path, skip_root_check: bool = False) -> None:
        """带 Range 的媒体文件服务（试听/预览）。仅放行媒体后缀 + 白名单根目录。"""
        try:
            file = file.expanduser().resolve()
        except OSError:
            self._json({"error": "路径非法"}, 400)
            return
        if file.suffix.lower() not in _MEDIA_SUFFIXES:
            self._json({"error": f"不支持的媒体类型：{file.suffix}"}, 403)
            return
        if not skip_root_check and not _media_allowed(file):
            self._json({"error": "该路径不在数据总目录/输出目录内，拒绝访问。"}, 403)
            return
        if not file.is_file():
            self._json({"error": f"文件不存在：{file}"}, 404)
            return
        size = file.stat().st_size
        ctype = _MEDIA_SUFFIXES[file.suffix.lower()]
        start, end = 0, size - 1
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            piece = header[6:].split(",")[0].strip()
            try:
                left, _, right = piece.partition("-")
                start = int(left) if left else max(0, size - int(right))
                end = min(int(right), size - 1) if (left and right) else end
            except ValueError:
                start, end = 0, size - 1
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        partial = header is not None and (start, end) != (0, size - 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        length = end - start + 1
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with file.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionError):
                    return
                remaining -= len(chunk)

    def do_DELETE(self) -> None:
        route = urlparse(self.path).path
        # License gate（DELETE 全走 gate；无 license 的公开 DELETE）
        if _license_should_block(route):
            self._json({
                "error": "license_required",
                "message": "请先输入激活码激活软件。",
                "state": self._license_state_json(),
            }, 403)
            return
        if route.startswith("/api/clones/"):
            name = Path(unquote(route.rsplit("/", 1)[-1])).name  # 只允许文件名，防目录穿越
            target = studio_settings.clones_dir() / name
            target.unlink(missing_ok=True)
            target.with_suffix(".json").unlink(missing_ok=True)
            self._json({"ok": True})
            return
        if route.startswith("/api/assets/"):
            name = Path(unquote(route.rsplit("/", 1)[-1])).name
            (studio_settings.audio_assets_dir() / name).unlink(missing_ok=True)
            self._json({"ok": True})
            return
        if route.startswith("/api/config/"):
            name = unquote(route.rsplit("/", 1)[-1])
            _delete_config_preset(name)
            self._json({"ok": True, "presets": _load_config_presets()})
            return
        if route.startswith("/api/voices/"):
            voice_library.delete_voice(_vroot(), unquote(route.rsplit("/", 1)[-1]))
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, 404)


# ------------------------------------------------------------------ 媒体白名单与克隆存档
_MEDIA_SUFFIXES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
}
_EXTRA_MEDIA_ROOTS: set[str] = set()


def _allow_media_root(path_text: str) -> None:
    path_text = path_text.strip()
    if path_text:
        try:
            _EXTRA_MEDIA_ROOTS.add(str(Path(path_text).expanduser().resolve()))
        except OSError:
            pass


def _media_allowed(file: Path) -> bool:
    roots = set(_EXTRA_MEDIA_ROOTS)
    try:
        roots.add(str(studio_settings.data_root().resolve()))
    except OSError:
        pass
    if STUDIO_HOME is not None:
        roots.add(str(STUDIO_HOME.resolve()))
    text = str(file)
    return any(text == root or text.startswith(root.rstrip("\\/") + sep)
               for root in roots for sep in ("\\", "/"))


def _config_presets_file() -> Path:
    """作品参数预设：存进总目录，跟数据一起可迁移（统一作品参数设定）。"""
    return studio_settings.data_root() / "作品参数预设.json"


def _load_config_presets() -> dict:
    try:
        data = json.loads(_config_presets_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_config_preset(name: str, config: dict) -> None:
    presets = _load_config_presets()
    presets[name] = config
    _config_presets_file().write_text(json.dumps(presets, ensure_ascii=False, indent=2),
                                      encoding="utf-8")


def _delete_config_preset(name: str) -> None:
    presets = _load_config_presets()
    if name in presets:
        del presets[name]
        _config_presets_file().write_text(json.dumps(presets, ensure_ascii=False, indent=2),
                                          encoding="utf-8")


def _safe_name(text: str) -> str:
    """文件名安全化（去掉路径分隔与非法字符）。"""
    import re as _re

    return _re.sub(r"[^\w一-鿿-]+", "_", str(text)).strip("_") or "voice"


def _as_number(raw, default):
    """P0-3：从 payload 里取数字**并严格保留 0**——None/空串走默认，数字 0/负数
    /小数原样返回，让 SynthesisOptions 校验去接手明确报错（不再被 `or default` 吞掉）。
    非数字字符串（例如 "abc"）当作缺省，避免让 float("abc") 直接崩溃 UI。"""
    if raw is None:
        return default
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return default
        try:
            v = float(s)
        except ValueError:
            return default
        return v
    if isinstance(raw, (int, float)):
        return raw
    return default


def _resolve_asset(filename: str) -> Path | None:
    """把 BGM/音效文件名映射到配乐音效目录下的真实路径（只认文件名，防穿越）。"""
    if not filename:
        return None
    target = studio_settings.audio_assets_dir() / Path(filename).name
    return target if target.is_file() else None


def _assets_payload() -> list[dict]:
    """配乐音效素材清单（BGM 与音效共用一个素材库，前端自行分派用途）。"""
    items = []
    root = studio_settings.audio_assets_dir()
    for file in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if file.suffix.lower() in _MEDIA_SUFFIXES and file.is_file():
            items.append({"file": file.name,
                          "url": "/api/asset?file=" + quote(file.name),
                          "size_mb": round(file.stat().st_size / (1024 * 1024), 2)})
    return items


def _chunks_payload(output_dir: str) -> dict:
    """成片的配音分段清单（逐段试听/重配用）。每段给 text/seconds/音频 url。"""
    if not output_dir.strip():
        return {"chunks": []}
    _allow_media_root(output_dir)  # 放行该输出目录，供 /api/audio 播放分段
    master = Path(output_dir) / pipeline.MASTER_NAME
    from .engines.longform import chunks_dir_for, read_manifest

    manifest = read_manifest(master)
    if not manifest:
        return {"chunks": []}
    cdir = chunks_dir_for(master)
    out = []
    for c in manifest.get("chunks", []):
        fp = cdir / str(c.get("file") or "")
        out.append({"index": c.get("index"), "text": c.get("text", ""),
                    "seconds": c.get("seconds", 0),
                    "url": "/api/audio?path=" + quote(str(fp)) if fp.is_file() else ""})
    return {"chunks": out, "master_url": "/api/audio?path=" + quote(str(master)) if master.is_file() else ""}


def _clones_payload() -> list[dict]:
    """克隆音频存档清单（新→旧）：文件名即 时间戳_引擎_音色。"""
    items = []
    root = studio_settings.clones_dir()
    for file in root.iterdir():
        if file.suffix.lower() in _MEDIA_SUFFIXES and file.is_file():
            stat = file.stat()
            items.append({"file": file.name, "path": str(file),
                          "size_mb": round(stat.st_size / (1024 * 1024), 2),
                          "mtime": stat.st_mtime})
    return sorted(items, key=lambda x: x["mtime"], reverse=True)


def _open_folder(target: Path) -> None:
    import os
    import subprocess
    import sys as _sys

    if os.name == "nt":
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif _sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _browse(path_text: str, files_ext: str = "") -> dict:
    """本机浏览：选文件夹用；files_ext 非空（如 "xlsx,txt"）时**同时列出匹配文件**（选文档用）。

    空路径给根列表（Windows 盘符 / Linux 根）。返回 dirs（子目录）+ files（匹配文件，仅在
    files_ext 给定时）。文件项供「导入表格/TXT」在本机选文档 → 拿到真实路径 → 反推所在文件夹。"""
    import os
    import string

    exts = tuple("." + e.strip().lower().lstrip(".") for e in files_ext.split(",") if e.strip())
    if not path_text.strip():
        if os.name == "nt":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return {"path": "", "parent": None, "dirs": drives, "files": [], "roots": True}
        path_text = "/"
    current = Path(path_text)
    if not current.is_dir():
        return {"error": f"目录不存在：{current}", "path": str(current), "dirs": [], "files": []}
    dirs, files = [], []
    try:
        for child in sorted(current.iterdir(), key=lambda x: x.name.lower()):
            if child.name.startswith((".", "$")):
                continue
            if child.is_dir():
                dirs.append(child.name)
            elif exts and child.is_file() and child.name.lower().endswith(exts):
                files.append(child.name)
    except OSError as exc:   # 无权限/断链/ELOOP 等都返回 JSON，不要抛成 500 断连
        return {"error": f"无法访问：{current}（{exc.__class__.__name__}）", "path": str(current), "dirs": [], "files": []}
    parent = str(current.parent) if current.parent != current else ""
    return {"path": str(current), "parent": parent, "dirs": dirs, "files": files, "roots": False}


def _state_payload() -> dict:
    from .version import full_version

    voices = [{"voice_id": v.voice_id, "name": v.name, "transcript": v.transcript[:40],
               "engine": v.engine, "params": v.params, "released": v.released}
              for v in voice_library.list_voices(_vroot())]
    # v0.7.71 P0-1：把每个引擎的 EngineCapabilities 序列化返回，供前端 UI 显示
    # "当前引擎原生支持什么 / 哪些由软件后处理"提示，避免用户以为参数没生效。
    engine_capabilities: dict[str, dict] = {}
    for key in pipeline.ENGINE_KEYS:
        try:
            _eng = pipeline.make_engine(key)
            caps = getattr(_eng, "capabilities", None)
            if caps is None:
                continue
            engine_capabilities[key] = {
                "native_speed": bool(caps.native_speed),
                "supports_seed": bool(caps.supports_seed),
                "supports_num_steps": bool(caps.supports_num_steps),
                "supports_guidance": bool(caps.supports_guidance),
                "supports_edge_pitch": bool(caps.supports_edge_pitch),
                "supports_edge_style": bool(caps.supports_edge_style),
                "supports_voice_ref": bool(caps.supports_voice_ref),
                "detail": caps.detail,
            }
        except Exception:  # noqa: BLE001
            pass  # 某引擎构造失败不影响其它引擎能力返回
    return {
        "version": full_version(),
        # 云配版曝光：dots_remote + edge_tts；正式版继续曝光全部（含 edge_tts）
        "engines": (list(pipeline.CLOUD_ENGINE_KEYS) if studio_settings.cloud_only()
                     else list(pipeline.ENGINE_KEYS)),
        "cloud_only": studio_settings.cloud_only(),
        "aligners": pipeline.ALIGNER_KEYS,
        "aspects": pipeline.ASPECT_KEYS,
        "positions": list(POSITION_PRESETS),
        "voices": voices,
        "voice_root": str(voice_library.voices_root(_vroot())),
        # Edge TTS 预设声线 / 风格常量（前端下拉源；不进 voice_library，不需要参考音频）
        "edge_voices": edge_tts_mod.voice_choices(),
        "edge_styles": edge_tts_mod.style_choices(),
        "edge_default_style": edge_tts_mod.DEFAULT_STYLE,
        "edge_endpoint": edge_tts_mod.edge_tts_endpoint(),
        "engine_capabilities": engine_capabilities,
    }


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> ThreadingHTTPServer:
    # 授权：启动时若本地已有激活状态，尝试静默 start（拿新的 session_token
    # 并启动心跳线程）；网络失败/授权失效都不阻塞 server 启动 —— 用户会
    # 看到"未激活"页面并被引导重新输入激活码。
    # RASP 检测：strict 模式下检测到就 sys.exit(3)。
    # 不再用 try/except Exception 包住整个 RASP 分支（那样任何异常都会被吞掉，
    # 攻击者只需触发一个 rasp.py 里的 import 错误就能绕过）。
    # 单独 try 保 mgr.rasp_scan()（怕外部工具异常），但 exit 语句本身不被吞。
    # 【关键顺序调整】socket bind 必须先于 licensing 网络 IO，
    # 否则 licensing 服务器慢/不通 → start_from_saved 挂 30 秒 → socket 从没 bind
    # → pywebview 抢先请求 URL 拿到 ERR_EMPTY_RESPONSE（用户看到的白屏×）
    server = None
    last_error: OSError | None = None
    for candidate in range(port, port + 20):  # 端口被占自动顺延，避免双击闪退
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
            port = candidate
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise OSError(f"端口 {port}~{port + 19} 均被占用：{last_error}")

    # ---------- 先立即 RASP 硬检查（打包版 strict 命中就退，不给启动机会） ----------
    try:
        mgr = _licensing_pkg.get_manager()
    except Exception as _exc:  # noqa: BLE001
        print(f"[FATAL] licensing 子系统初始化失败：{_exc}")
        try:
            from . import _build_info as _bi
            if getattr(_bi, "PACKAGED", False):
                import sys as _sys
                _sys.exit(4)
        except ImportError:
            pass
        mgr = None

    if mgr is not None:
        report = None
        try:
            report = mgr.rasp_scan()
        except Exception as _exc:  # noqa: BLE001
            print(f"[RASP] scan raised: {_exc}")
        strict = _licensing_pkg.rasp.strict_mode_enabled()
        if report is not None and report.suspicious and strict:
            print(
                f"[FATAL] RASP 检测到高风险环境：{report.summary()}，软件退出。"
            )
            import sys as _sys
            _sys.exit(3)

        # ---------- start_from_saved 放**后台线程** ----------
        # 网络失败不再挂住主启动（现在 is_active 也是激活过就放行，
        # 不再要 session_token 一定拿到手）
        def _lic_boot():
            try:
                mgr.start_from_saved()
            except Exception as _exc:  # noqa: BLE001
                print(f"[license] start_from_saved (bg): {_exc}")
        threading.Thread(target=_lic_boot, daemon=True,
                         name="license-boot").start()
    from .version import full_version

    url = f"http://127.0.0.1:{port}/"
    print(f"水星配音对齐工作室 {full_version()} 已启动：{url}（Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server


def main(port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    server = serve(port, open_browser)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        # R11-3：应用退出时只在 bulk service 已被使用过时安全停止它——
        # 不为了停止而 get_service() 触发创建
        try:
            from .bulk_dub import service as _bulk_service

            existing = _bulk_service.peek_service()
            if existing is not None:
                existing.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0
