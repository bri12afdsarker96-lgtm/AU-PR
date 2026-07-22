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
from urllib.parse import parse_qs, unquote, urlparse

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

    def snapshot(self) -> dict:
        with self.lock:
            timings = [
                {"index": t.index, "text": t.text, "duration": t.duration}
                for t in (self.timings or [])
            ]
            return {"id": self.slot, "label": self.label,
                    "running": self.running, "done": self.done, "ok": self.ok,
                    "action": self.action, "log": list(self.log), "timings": timings}

    def append(self, message: str) -> None:
        with self.lock:
            self.log.append(message)


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
        options = SynthesisOptions(
            speed=float(payload.get("speed") or 1.0),
            max_pause_seconds=float(payload.get("max_pause") or 0.0),
            seed=int(payload.get("seed") or 42),
        )
        voice = None
        if payload.get("voice_id"):
            voice = voice_library.get_voice(_vroot(), str(payload["voice_id"])).to_ref()
        style = None
        if payload.get("burn_subtitles", True):
            style = SubtitleStyle(
                font_size_px=int(payload.get("subtitle_size") or 64),
                position=str(payload.get("subtitle_position") or "底部"),
                font_name=str(payload.get("subtitle_font") or ""),
            )
        overlays = overlays_from_dicts(payload.get("overlays") or [])
        aspect = str(payload.get("aspect") or pipeline.DEFAULT_ASPECT)
        config = pipeline.make_render_config(aspect)
        canvas = (config.width, config.height)

        if action == "run_all":
            run = pipeline.run_all(
                text, engine_key, aligner_key,
                Path(str(payload.get("shots_dir") or "")), output_dir,
                voice=voice, options=options, subtitle_style=style,
                export_capcut=bool(payload.get("export_capcut")), overlays=overlays,
                config=config,
            )
            for note in run.notes:
                log(f"⚠ {note}")
            for shot in run.result.shots:
                log(f"  {shot.index:>2} | 音频{shot.target_seconds:>6.2f}s | {shot.strategy}")
            log(f"字幕/文本框：{run.result.subtitle_note or '未启用'}")
            if run.capcut:
                log(f"剪映：{run.capcut.message}")
            log(("✅ 成片完成：" if run.ok else "❌ 收口断言未通过：") + str(run.result.output_path))
            with JOB.lock:
                JOB.timings, JOB.result, JOB.ok = run.timings, run.result, run.ok
        elif action == "dub":
            master = pipeline.step_dub(text, engine_key, output_dir, voice, options)
            log(f"✅ master：{master.path.name}（{master.seconds:.2f}s，引擎 {master.engine}）")
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
            result = pipeline.step_render(output_dir / pipeline.MASTER_NAME, timings, videos,
                                          output_dir, style, config=config, overlays=overlays)
            log(f"字幕/文本框：{result.subtitle_note or '未启用'}")
            log(("✅ 成片完成：" if result.ok else "❌ 收口断言未通过：") + str(result.output_path))
            with JOB.lock:
                JOB.result, JOB.ok = result, result.ok
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

            root = studio_settings.data_root()
            log(f"总目录：{root}")
            for label, path in (("组件", studio_settings.components_root()),
                                ("音色库", voice_library.voices_root(_vroot())),
                                ("克隆音频", root / studio_settings.DIR_CLONES),
                                ("字体", root / studio_settings.DIR_FONTS)):
                log(f"  {label}：{path}（{'存在' if path.is_dir() else '将在首次使用时创建'}）")
            ff = bool(_sh.which("ffmpeg") and _sh.which("ffprobe"))
            log(("✅ " if ff else "⛔ ") + "ffmpeg / ffprobe" + ("" if ff else "：未找到，请放到软件目录旁或加入 PATH"))
            for c in toolbox.component_statuses():
                log(("✅ " if c["installed"] else "⛔ ") + f"{c['name']}：{c['detail']}")
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
            self._json({"data_root": str(studio_settings.data_root()),
                        "default_data_root": str(studio_settings.default_data_root()),
                        "component_root": str(studio_settings.component_root())})
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
                entry = voice_library.register_voice(
                    _vroot(), query.get("name") or "未命名",
                    temp, transcript=query.get("transcript") or "")
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

    voices = [{"voice_id": v.voice_id, "name": v.name, "transcript": v.transcript[:40]}
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
