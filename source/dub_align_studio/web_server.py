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
from .engines import DotsLocalEngine, DotsRemoteEngine, FishLocalEngine, MockEngine, SynthesisOptions
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


def _run_job(JOB: JobState, action: str, payload: dict) -> None:  # noqa: N803 —— 沿用旧函数体的 JOB 名
    log = JOB.append
    try:
        text = str(payload.get("text") or "")
        output_dir = Path(str(payload.get("output_dir") or "")) if payload.get("output_dir") else None
        engine_key = str(payload.get("engine") or "mock")
        aligner_key = str(payload.get("aligner") or "均分兜底")
        voice = None
        vparams: dict = {}
        if payload.get("voice_id"):
            entry = voice_library.get_voice(_vroot(), str(payload["voice_id"]))
            voice = entry.to_ref()
            vparams = entry.params or {}
        # 合成参数：所选音色设计的 num_steps/guidance 生效于成片；速度/种子/停顿以界面为准（界面有滑杆）
        options = SynthesisOptions(
            num_steps=int(payload.get("num_steps") or vparams.get("num_steps") or 10),
            guidance_scale=float(payload.get("guidance_scale") or vparams.get("guidance_scale") or 1.2),
            speed=float(payload.get("speed") or vparams.get("speed") or 1.0),
            max_pause_seconds=float(payload.get("max_pause") or 0.0),
            seed=int(payload.get("seed") or vparams.get("seed") or 42),
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

        audio_mix = mix_from_payload(payload.get("audio") or {}, _resolve_asset)

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
            with _DUB_LOCK:   # 配音阶段串行：GPU 一次只跑一个克隆；期间上一个任务可并行渲染
                if reuse_dub:
                    # 复用已有配音：只改了音量/BGM/字幕/进度条时，跳过整段克隆，直接量时长+重渲染（秒出）
                    from .engines.base import wav_seconds

                    JOB.set_progress("① 复用已有配音（跳过克隆）", 76)
                    log(f"① 复用已有配音：master.wav 已存在（{wav_seconds(master_path):.2f}s），跳过克隆，仅重渲染。")
                else:
                    JOB.set_progress("① 配音 · 逐行克隆", 5)
                    log("① 配音 · 逐行克隆…")

                    def _dub_progress(done: int, total: int) -> None:
                        JOB.check_cancel()   # 队列任务被取消 → 逐行处理间隙中止
                        pct = 5 + int(done / max(1, total) * 73)  # 配音占 5~78%
                        JOB.set_progress(f"① 配音 · 第 {done}/{total} 行", pct)

                    def _dub_beat(stage: str = "") -> None:
                        JOB.check_cancel()
                        with JOB.lock:
                            if stage:
                                JOB.stage = stage
                            JOB.last_tick = time.time()

                    master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                               progress=_dub_progress, heartbeat=_dub_beat)
                    log(f"  ✅ master {master.seconds:.2f}s（引擎 {master.engine}）")

            JOB.check_cancel()   # 配音后、量时长前的取消检查点
            JOB.set_progress("② 量时长 · 排队中（等待渲染槽）…", 77)
            with _RENDER_LOCK:   # 量时长+渲染阶段串行：本地 ffmpeg/whisper 一次只一个；期间下个任务可并行配音
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
                with JOB.lock:
                    JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
                if result.ok:
                    _remember_edit_item(output_dir, canvas, payload)  # ⑧ 进待编辑队列
        elif action == "dub":
            def _dub_progress(done: int, total: int) -> None:
                JOB.set_progress(f"配音 · 第 {done}/{total} 行", int(done / max(1, total) * 100))

            def _dub_beat(stage: str = "") -> None:
                with JOB.lock:
                    if stage:
                        JOB.stage = stage
                    JOB.last_tick = time.time()

            master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                       progress=_dub_progress, heartbeat=_dub_beat)
            log(f"✅ master：{master.path.name}（{master.seconds:.2f}s，引擎 {master.engine}）")
            with JOB.lock:
                JOB.ok = True
        elif action == "rechunk":
            # 只重配某一段（避免整篇重来的死循环）：重合成该段 → 重拼 master
            from .engines.longform import redub_chunk

            engine = pipeline.make_engine(engine_key)
            index = int(payload.get("chunk_index") or 0)
            redub_chunk(engine, output_dir / pipeline.MASTER_NAME, index, voice, options, log=log)
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
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
            if result.ok:
                _remember_edit_item(output_dir, canvas, payload)  # ⑧ 刷新待编辑队列时效
        elif action == "capcut":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            # JOB.result 为 None（软件重启后）时由 step_capcut 从磁盘读回分镜段
            package = pipeline.step_capcut(timings, JOB.result, output_dir / pipeline.MASTER_NAME,
                                           output_dir, style or SubtitleStyle(), canvas=canvas)
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
                                               config.fps, config.width, config.height)
            log(f"✅ Premiere 交换工程已导出：{xml_path}")
            log("   ⚠ 用 Premiere「文件 → 导入」选该 .xml（不是「打开项目」——打开只认 .prproj，会提示格式不正确）。")
            log("   导入后即得完整时间线（V1 分镜段 + A1 整轨配音）；想要 .prproj 就在 PR 里「另存为」。字幕另导入 成片.srt。")
            log("   素材已复制进「Premiere工程_素材/」，工程自包含、可整体拷走，清理缓存后仍可导入。")
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
            for status in (MockEngine().probe(), DotsLocalEngine().probe(),
                           DotsRemoteEngine().probe(), FishLocalEngine().probe()):
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
            opts = SynthesisOptions(
                num_steps=int(float(payload.get("num_steps") or 10)),
                guidance_scale=float(payload.get("guidance_scale") or 1.2),
                speed=float(payload.get("speed") or 1.0),
                max_pause_seconds=float(payload.get("max_pause_seconds") or 0.0),
                seed=int(float(payload.get("seed") or 42)),
            )
            voice_id = str(payload.get("voice_id") or "默认声线")
            sig = hashlib.md5(  # noqa: S324 —— 仅做缓存键，非安全用途
                f"{voice_id}|{engine_key}|{opts.to_payload()}|{sample}".encode("utf-8")
            ).hexdigest()[:10]
            cache_dir = studio_settings.clones_dir() / "试听缓存"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{_safe_name(voice_id)}_{engine_key}_{sig}.wav"
            if cached.is_file() and cached.stat().st_size > 44:
                log(f"✅ 命中试听缓存，直接复用（未重复渲染）：{cached.name}")
                with JOB.lock:
                    JOB.ok = True
                    JOB.result = {"try_audio": str(cached)}
            else:
                log(f"用引擎 {engine_key} 合成试听句（{len(sample)} 字）…首次合成后会缓存，之后试听秒开。")
                master = pipeline.make_engine(engine_key).synthesize_full(sample, voice, cached, opts)
                with JOB.lock:
                    JOB.ok = True
                    JOB.result = {"try_audio": str(master.path)}
                log(f"✅ 试听已生成并缓存：{master.path.name}（{master.seconds:.2f}s）")
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
            import shutil as _sh

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
            ff = bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))
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
        with JOB.lock:
            JOB.running = False
            JOB.done = True


# ------------------------------------------------------------------ HTTP
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # 静默访问日志
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urlparse(self.path).path
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
        if route == "/api/probe":
            statuses = [
                {"key": s.key, "name": n, "available": s.available, "detail": s.detail}
                for s, n in (
                    (MockEngine().probe(), "mock 引擎"),
                    (DotsLocalEngine().probe(), "dots.tts"),
                    (DotsRemoteEngine().probe(), "dots.tts 云端"),
                    (FishLocalEngine().probe(), "fish-speech"),
                )
            ]
            aligner = WhisperAligner().probe()
            statuses.append({"key": aligner.key, "name": "whisper 尺子",
                             "available": aligner.available, "detail": aligner.detail})
            import shutil as _sh

            ff = bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))
            statuses.insert(0, {"key": "ffmpeg", "name": "ffmpeg / ffprobe",
                                "available": ff,
                                "detail": "渲染就绪。" if ff else "未找到 ffmpeg/ffprobe，无法渲染成片。"})
            self._json({"components": statuses})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
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
                self._json(out)
            except Exception as exc:
                self._json({"error": f"保存失败：{exc}"}, 400)
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
    return {
        "version": full_version(),
        "engines": pipeline.ENGINE_KEYS,
        "aligners": pipeline.ALIGNER_KEYS,
        "aspects": pipeline.ASPECT_KEYS,
        "positions": list(POSITION_PRESETS),
        "voices": voices,
        "voice_root": str(voice_library.voices_root(_vroot())),
    }


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> ThreadingHTTPServer:
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
    return 0
