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

import json
import tempfile
import threading
import traceback
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import components as toolbox
from . import fonts as font_library
from . import settings as studio_settings
from . import studio_pipeline as pipeline
from . import voice_library
from . import xlsx_reader
from .aligners import WhisperAligner
from .engines import DotsLocalEngine, FishLocalEngine, MockEngine, SynthesisOptions
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
        else:
            job = JobState(slot=slot, label=label, running=True, action=action,
                           log=[f"══ {label} 开始 ══"])
            TASKS[slot] = job
    threading.Thread(target=_run_job, args=(job, action, payload), daemon=True).start()
    return job, ""


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
                border_width=int(payload.get("subtitle_border", 3)),
            )
        overlays = overlays_from_dicts(payload.get("overlays") or [])
        aspect = str(payload.get("aspect") or pipeline.DEFAULT_ASPECT)
        config = pipeline.make_render_config(aspect)
        canvas = (config.width, config.height)
        from .audio_mix import mix_from_payload

        audio_mix = mix_from_payload(payload.get("audio") or {}, _resolve_asset)

        # 友好校验：目录留空时给明确提示，避免 Path(None) 抛 TypeError（用户反馈①）
        if action in ("run_all", "dub", "timing", "render", "capcut", "rechunk", "finalize") and output_dir is None:
            raise ValueError("请先在下方选择「输出目录」（配音与成片都写到这里）。")
        if action in ("run_all", "render", "finalize") and not str(payload.get("shots_dir") or "").strip():
            raise ValueError("请先选择「分镜目录」（放 1.mp4、2.mp4 … 的文件夹）。")
        if action in ("run_all", "dub", "timing") and not text.strip():
            raise ValueError("请先填写或导入「待合成文案」。")

        if action == "run_all":
            from integrated_workbench.semantic_match import parse_script

            shots_dir = Path(str(payload.get("shots_dir") or ""))
            lines = parse_script(text)
            videos = pipeline.list_shot_videos(shots_dir)
            if len(videos) < len(lines):
                raise ValueError(f"分镜视频不足：文案 {len(lines)} 行，目录里只有 {len(videos)} 个视频。")
            videos = videos[: len(lines)]

            JOB.set_progress("① 配音 · 逐行克隆", 6)
            log("① 配音 · 逐行克隆…")

            def _dub_progress(done: int, total: int) -> None:
                pct = 6 + int(done / max(1, total) * 22)  # 配音占 6~28%，逐行推进
                JOB.set_progress(f"① 配音 · 第 {done}/{total} 行", pct)

            master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                       progress=_dub_progress)
            log(f"  ✅ master {master.seconds:.2f}s（引擎 {master.engine}）")

            JOB.set_progress("② 量时长 · 逐行对齐", 28)
            log("② 量时长 · 逐行对齐…")
            timings, notes = pipeline.step_timing(text, master.path, aligner_key, output_dir)
            for note in notes:
                log(f"  ⚠ {note}")
            with JOB.lock:
                JOB.timings = timings

            JOB.set_progress("③ 渲染成片 · 逐行收口", 38)
            log("③ 渲染成片 · 逐行裁剪/变速 + 整轨叠加…")

            def _render_progress(done: int, total: int) -> None:
                pct = 38 + int(done / max(1, total) * 52)
                JOB.set_progress(f"③ 渲染成片 · 第 {done}/{total} 段", pct)

            result = pipeline.step_render(master.path, timings, videos, output_dir, style,
                                          config=config, overlays=overlays, audio_mix=audio_mix,
                                          progress=_render_progress)
            log(f"  字幕/文本框：{result.subtitle_note or '未启用'}")
            capcut = None
            if payload.get("export_capcut"):
                JOB.set_progress("④ 导出剪映草稿", 93)
                log("④ 导出剪映草稿…")
                capcut = pipeline.step_capcut(timings, result, master.path, output_dir, style,
                                              canvas=canvas)
                log(f"  剪映：{capcut.message}")
            JOB.set_progress("完成", 100)
            log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
        elif action == "dub":
            def _dub_progress(done: int, total: int) -> None:
                JOB.set_progress(f"配音 · 第 {done}/{total} 行", int(done / max(1, total) * 100))

            master = pipeline.step_dub(text, engine_key, output_dir, voice, options, log=log,
                                       progress=_dub_progress)
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
            videos = pipeline.list_shot_videos(Path(str(payload.get("shots_dir") or "")))[: len(timings)]

            def _render_progress(done: int, total: int) -> None:
                JOB.set_progress(f"渲染成片 · 第 {done}/{total} 段", int(done / max(1, total) * 100))

            result = pipeline.step_render(output_dir / pipeline.MASTER_NAME, timings, videos,
                                          output_dir, style, config=config, overlays=overlays,
                                          audio_mix=audio_mix, progress=_render_progress)
            log(f"字幕/文本框：{result.subtitle_note or '未启用'}")
            log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
            with JOB.lock:
                JOB.result, JOB.ok = result, result.ok
        elif action == "finalize":
            # 文本框逐行校对后：用校对文字重烧字幕（overlays）+ 导出剪映草稿，一步到位（导出统一在文本框页触发）
            master_path = output_dir / pipeline.MASTER_NAME
            timings = JOB.timings or pipeline.load_timings(output_dir)
            videos = pipeline.list_shot_videos(Path(str(payload.get("shots_dir") or "")))[: len(timings)]

            def _fin_progress(done: int, total: int) -> None:
                JOB.set_progress(f"重烧字幕 · 第 {done}/{total} 段", 8 + int(done / max(1, total) * 80))

            JOB.set_progress("① 重烧字幕 · 逐行收口", 8)
            log("① 用文本框校对后的逐行文字重烧字幕成片…")
            result = pipeline.step_render(master_path, timings, videos, output_dir, style,
                                          config=config, overlays=overlays, audio_mix=audio_mix,
                                          progress=_fin_progress)
            log(("  ✅ 成片：" if result.ok else "  ❌ 收口未过：") + str(result.output_path))
            JOB.set_progress("② 导出剪映草稿", 92)
            log("② 导出剪映草稿…")
            package = pipeline.step_capcut(timings, result, master_path, output_dir,
                                           style or SubtitleStyle(), canvas=canvas)
            log(f"  ✅ {package.message}")
            log(f"     交接包：{package.package_dir}")
            JOB.set_progress("完成", 100)
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = timings, result, result.ok
        elif action == "capcut":
            timings = JOB.timings or pipeline.load_timings(output_dir)
            # JOB.result 为 None（软件重启后）时由 step_capcut 从磁盘读回分镜段
            package = pipeline.step_capcut(timings, JOB.result, output_dir / pipeline.MASTER_NAME,
                                           output_dir, style or SubtitleStyle(), canvas=canvas)
            log(f"✅ {package.message}")
            log(f"   交接包：{package.package_dir}")
            with JOB.lock:
                JOB.ok = True
        elif action == "probe":
            for status in (MockEngine().probe(), DotsLocalEngine().probe(), FishLocalEngine().probe()):
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
            self._json(_browse(query.get("path") or ""))
            return
        if route == "/api/settings":
            root = studio_settings.data_root()
            self._json({"data_root": str(root),
                        "default_data_root": str(studio_settings.default_data_root()),
                        "component_root": str(studio_settings.component_root()),
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
                raw = str(payload.get("data_root") or payload.get("component_root") or "")
                root = studio_settings.set_data_root(raw)
                self._json({"ok": True, "data_root": str(root)})
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
            try:
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
                self._json({"ok": True, "lines": lines})
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


def _browse(path_text: str) -> dict:
    """本机目录浏览（选择文件夹用）：空路径给根列表（Windows 盘符 / Linux 根）。"""
    import os
    import string

    if not path_text.strip():
        if os.name == "nt":
            drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
            return {"path": "", "parent": None, "dirs": drives, "roots": True}
        path_text = "/"
    current = Path(path_text)
    if not current.is_dir():
        return {"error": f"目录不存在：{current}", "path": str(current), "dirs": []}
    dirs = []
    try:
        for child in sorted(current.iterdir(), key=lambda x: x.name.lower()):
            if child.is_dir() and not child.name.startswith((".", "$")):
                dirs.append(child.name)
    except PermissionError:
        return {"error": f"无权限访问：{current}", "path": str(current), "dirs": []}
    parent = str(current.parent) if current.parent != current else ""
    return {"path": str(current), "parent": parent, "dirs": dirs, "roots": False}


def _state_payload() -> dict:
    from .version import full_version

    voices = [{"voice_id": v.voice_id, "name": v.name, "transcript": v.transcript[:40],
               "engine": v.engine, "params": v.params}
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
