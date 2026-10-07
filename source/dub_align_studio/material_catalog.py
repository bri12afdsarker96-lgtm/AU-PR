"""Read-only media library inventory stored in a caller-owned SQLite cache."""

from __future__ import annotations

import hashlib
import json
import math
import ntpath
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISLNK

from integrated_workbench.proc import run_silent

from . import settings, xlsx_reader
from .material_select import VIDEO_EXTENSIONS


SCHEMA_VERSION = 3
_EXCLUDED_FOLDERS = {"待人工复核", "非宇宙内容"}


class ProbeUnavailableError(RuntimeError):
    """The probing environment is unavailable; asset health is not established."""


def _ffprobe_payload(path: Path) -> dict:
    try:
        completed = run_silent(
            [settings.ffmpeg_tool("ffprobe"), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("ffprobe 探测超时（30 秒）") from exc
    except OSError as exc:
        raise ProbeUnavailableError(
            f"无法启动 ffprobe，请安装 FFmpeg 或使用「一键修复ffmpeg.bat」，"
            f"并检查程序路径及执行权限: {exc}"
        ) from exc
    if completed.returncode != 0:
        raise ValueError(f"ffprobe 读取失败（退出码 {completed.returncode}）: "
                         f"{(completed.stderr or '').strip()[-500:] or '无错误详情'}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ffprobe 输出不是有效 JSON: {exc}") from exc


def _positive_number(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}无效: {value!r}") from exc
    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label}必须为有限正数: {value!r}")
    return number


def _frame_rate(video: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = video.get(key)
        if isinstance(raw, bool):
            raise ValueError("视频帧率不能是布尔值")
        if raw in (None, "", "N/A", "0/0", "0/1", "0", 0):
            continue
        parts = str(raw).split("/")
        if len(parts) > 2:
            raise ValueError(f"视频帧率格式无效: {raw!r}")
        numerator = _positive_number(parts[0], "视频帧率")
        denominator = _positive_number(parts[1], "视频帧率") if len(parts) == 2 else 1
        return _positive_number(numerator / denominator, "视频帧率")
    return None


def _codec_name(stream: dict) -> str | None:
    name = stream.get("codec_name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise ValueError(f"媒体编码名称无效: {name!r}")
    return name


def _media_metadata(payload: dict) -> tuple:
    if not isinstance(payload, dict):
        raise ValueError("ffprobe 元数据必须是对象")
    streams = payload.get("streams", [])
    if not isinstance(streams, list) or any(not isinstance(s, dict) for s in streams):
        raise ValueError("ffprobe streams 元数据必须是对象列表")
    video = None
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        disposition = stream.get("disposition", {})
        if not isinstance(disposition, dict):
            raise ValueError("ffprobe disposition 元数据必须是对象")
        if disposition.get("attached_pic", 0) not in (0, "0", False):
            continue
        video = stream
        break
    if video is None:
        raise ValueError("无可用视频流（音频或封面图片不能用作视频）")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    duration = video.get("duration")
    if duration is None or duration == "N/A":
        media_format = payload.get("format", {})
        if not isinstance(media_format, dict):
            raise ValueError("ffprobe format 元数据必须是对象")
        duration = media_format.get("duration")
    duration = _positive_number(duration, "视频时长")
    dimensions = [_positive_number(video.get(key), "视频宽高") for key in ("width", "height")]
    if any(not value.is_integer() or value > 2**31 - 1 for value in dimensions):
        raise ValueError("视频宽高必须为有效的正整数")
    return (duration, int(dimensions[0]), int(dimensions[1]), _frame_rate(video),
            _codec_name(video), _codec_name(audio))


def _valid_playable_cache(duration: object, width: object, height: object,
                          error: str | None, frame_rate: object = None,
                          video_codec: object = None, audio_codec: object = None, *,
                          size_bytes: int) -> bool:
    if error is not None or size_bytes <= 0:
        return False
    try:
        _positive_number(duration, "视频时长")
        if frame_rate is not None:
            _positive_number(frame_rate, "视频帧率")
        for codec in (video_codec, audio_codec):
            _codec_name({"codec_name": codec})
        dimensions = [_positive_number(value, "视频宽高") for value in (width, height)]
        return all(value.is_integer() and value <= 2**31 - 1 for value in dimensions)
    except ValueError:
        return False


def _iter_library_files(root: Path) -> Iterator[Path]:
    """Enumerate without suppressing directory I/O or entry-type errors."""
    pending = [root]
    while pending:
        directory = pending.pop()
        # Parent-file processing may have replaced a queued child with a link.
        if directory != root and S_ISLNK(os.lstat(directory).st_mode):
            continue
        files = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    if not entry.is_symlink():
                        pending.append(Path(entry.path))
                else:
                    files.append(Path(entry.path))
        # Close each directory handle before yielding files or descending.
        yield from files


@dataclass(frozen=True)
class AssetDescription:
    text: str
    source: str
    workbook_path: Path
    row_number: int
    column: str


@dataclass(frozen=True)
class Asset:
    asset_id: str
    path: Path
    relative_path: Path
    size_bytes: int
    mtime_ns: int
    probe_status: str
    duration_seconds: float | None
    width: int | None
    height: int | None
    frame_rate: float | None
    video_codec: str | None
    audio_codec: str | None
    probe_error: str | None = None
    descriptions: tuple[AssetDescription, ...] = ()


@dataclass(frozen=True)
class ScanStats:
    asset_count: int
    new_count: int
    changed_count: int
    unchanged_count: int


class Catalog:
    def __init__(self, db_path: Path, library: Path, *,
                 probe_func: Callable[[Path], dict] | None = None):
        """Inject a Path -> FFprobe JSON object callable, or use the bundled tool resolver.

        ProbeUnavailableError aborts and rolls back the scan; ValueError, OSError,
        and subprocess failures describe individual unreadable media files.
        """
        self._probe_func = probe_func if probe_func is not None else _ffprobe_payload
        self.library = Path(library).resolve()
        self.db_path = Path(db_path).resolve()
        if self.db_path.is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        self.root_path = ntpath.normcase(str(self.library))
        self.root_id = hashlib.sha256(self.root_path.encode("utf-8")).hexdigest()

    def refresh(self, probe: bool = True) -> ScanStats:
        """Refresh inventory and optionally health; unchanged completed probes are reused.

        A file changed during probing remains pending until a later refresh.
        The cache is updated atomically, including upgrades of older schemas.
        """
        if not self.library.is_dir():
            raise FileNotFoundError(self.library)
        assets = []
        seen_asset_ids = {}
        for path in _iter_library_files(self.library):
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            relative_path = path.relative_to(self.library)
            if (path.name == "成片.mp4" or any(
                part in _EXCLUDED_FOLDERS or part.endswith("_segments")
                for part in relative_path.parts[:-1]
            )):
                continue
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            asset_id = hashlib.sha256(
                f"{self.root_id}\0{ntpath.normcase(relative_path.as_posix())}".encode("utf-8")
            ).hexdigest()
            previous_path = seen_asset_ids.get(asset_id)
            if previous_path is not None and previous_path != relative_path.as_posix():
                raise ValueError(
                    f"素材路径归一化后冲突: {previous_path} 与 {relative_path.as_posix()}"
                )
            seen_asset_ids[asset_id] = relative_path.as_posix()
            assets.append((asset_id, relative_path.as_posix(), stat.st_size, stat.st_mtime_ns,
                           "pending" if probe else "skipped"))
        assets.sort(key=lambda row: row[1])

        if self.db_path.resolve().is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.resolve().is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                # Include schema upgrades in the same rollback boundary as health updates.
                conn.execute("BEGIN")
                conn.execute("""CREATE TABLE IF NOT EXISTS catalog_meta (
                schema_version INTEGER NOT NULL,
                root_path TEXT NOT NULL,
                scan_generation INTEGER NOT NULL DEFAULT 0
            )""")
                conn.execute("""CREATE TABLE IF NOT EXISTS assets (
                asset_id TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                probe_status TEXT NOT NULL,
                duration_seconds REAL,
                width INTEGER,
                height INTEGER,
                frame_rate REAL,
                video_codec TEXT,
                audio_codec TEXT,
                probe_error TEXT,
                scan_generation INTEGER NOT NULL DEFAULT 0
            )""")
                stored_meta = conn.execute(
                    "SELECT schema_version, root_path FROM catalog_meta"
                ).fetchone()
                if stored_meta is not None and stored_meta[1] != self.root_path:
                    raise ValueError("目录数据库对应的素材库不一致")
                if stored_meta is not None and stored_meta[0] not in (1, 2, SCHEMA_VERSION):
                    raise ValueError(f"不支持目录数据库 schema 版本: {stored_meta[0]}")
                if "scan_generation" not in {
                    row[1] for row in conn.execute("PRAGMA table_info(catalog_meta)")
                }:
                    conn.execute("ALTER TABLE catalog_meta ADD COLUMN scan_generation "
                                 "INTEGER NOT NULL DEFAULT 0")
                if "scan_generation" not in {
                    row[1] for row in conn.execute("PRAGMA table_info(assets)")
                }:
                    conn.execute("ALTER TABLE assets ADD COLUMN scan_generation "
                                 "INTEGER NOT NULL DEFAULT 0")
                if "probe_error" not in {
                    row[1] for row in conn.execute("PRAGMA table_info(assets)")
                }:
                    conn.execute("ALTER TABLE assets ADD COLUMN probe_error TEXT")
                previous_generation = conn.execute(
                    "SELECT scan_generation FROM catalog_meta"
                ).fetchone()
                generation = previous_generation[0] + 1 if previous_generation else 1
                conn.execute("DELETE FROM catalog_meta")
                conn.execute("INSERT INTO catalog_meta VALUES (?, ?, ?)",
                             (SCHEMA_VERSION, self.root_path, generation))
                existing = {
                    row[0]: row[1:]
                    for row in conn.execute(
                        """SELECT asset_id, relative_path, size_bytes, mtime_ns,
                            probe_status, duration_seconds, width, height, probe_error,
                            frame_rate, video_codec, audio_codec
                            FROM assets"""
                    )
                }
                new_count = changed_count = unchanged_count = 0
                for asset_id, relative_path, size_bytes, mtime_ns, probe_status in assets:
                    previous = existing.get(asset_id)
                    unchanged = (previous is not None
                                 and previous[1:3] == (size_bytes, mtime_ns)
                                 and previous[3] != "missing")
                    if previous is None:
                        conn.execute("""INSERT INTO assets (
                            asset_id, relative_path, size_bytes, mtime_ns, probe_status,
                            scan_generation
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                                     (asset_id, relative_path, size_bytes, mtime_ns,
                                      probe_status, generation))
                        new_count += 1
                    elif unchanged:
                        conn.execute("""UPDATE assets SET relative_path = ?,
                            scan_generation = ? WHERE asset_id = ?""",
                                     (relative_path, generation, asset_id))
                        unchanged_count += 1
                    else:
                        conn.execute("""UPDATE assets SET relative_path = ?,
                            size_bytes = ?, mtime_ns = ?, probe_status = ?,
                            scan_generation = ?,
                            duration_seconds = NULL, width = NULL, height = NULL,
                            frame_rate = NULL, video_codec = NULL, audio_codec = NULL,
                            probe_error = NULL
                            WHERE asset_id = ?""",
                                     (relative_path, size_bytes, mtime_ns, probe_status,
                                      generation, asset_id))
                        changed_count += 1
                    completed_probe = unchanged and (
                        previous[3] == "playable"
                        and _valid_playable_cache(*previous[4:], size_bytes=size_bytes)
                        or previous[3] == "unreadable" and bool(previous[7])
                    )
                    if probe and not completed_probe:
                        source = self.library / relative_path
                        try:
                            if size_bytes == 0:
                                raise ValueError("空文件，无法读取视频")
                            metadata = _media_metadata(self._probe_func(source))
                            status, error = "playable", None
                        except (ValueError, OSError, subprocess.SubprocessError) as exc:
                            metadata = (None,) * 6
                            status, error = "unreadable", f"媒体探测失败: {exc}"
                        try:
                            current_stat = source.stat()
                        except FileNotFoundError:
                            metadata = (None,) * 6
                            status, error = "missing", "探测期间文件消失"
                        else:
                            if (current_stat.st_size, current_stat.st_mtime_ns) != (size_bytes, mtime_ns):
                                metadata = (None,) * 6
                                status, error = "pending", "探测期间文件发生变化，需重新探测"
                                size_bytes, mtime_ns = current_stat.st_size, current_stat.st_mtime_ns
                        conn.execute("""UPDATE assets SET probe_status = ?, probe_error = ?,
                            duration_seconds = ?, width = ?, height = ?, frame_rate = ?,
                            video_codec = ?, audio_codec = ?, size_bytes = ?, mtime_ns = ?
                            WHERE asset_id = ?""",
                                     (status, error, *metadata, size_bytes, mtime_ns, asset_id))
                conn.execute("""UPDATE assets SET probe_status = 'missing'
                    WHERE scan_generation != ?""", (generation,))
        return ScanStats(asset_count=len(assets), new_count=new_count,
                         changed_count=changed_count, unchanged_count=unchanged_count)

    def import_legacy_metadata(self, path: Path) -> int:
        """Attach first-sheet E/G/H text using exact M-column BJ identifiers.

        The optional evidence table extends schemas 1-3 without changing inventory
        or health fields. Each import atomically replaces only this workbook's
        evidence. Refresh the catalog before importing. Returns the number
        of descriptions attached, counting each matching asset separately.
        """
        workbook = Path(path).resolve()
        workbook_key = ntpath.normcase(str(workbook))
        rows = xlsx_reader.read_columns(workbook, ("M", "E", "G", "H"), strip=False)
        with closing(sqlite3.connect(self._database_uri() + "?mode=rw", uri=True)) as conn:
            with conn:
                conn.execute("BEGIN")
                self._validate_binding(conn)
                conn.execute("""CREATE TABLE IF NOT EXISTS asset_descriptions (
                    asset_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    workbook_key TEXT NOT NULL,
                    workbook_path TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    column_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (asset_id, source, workbook_key, row_number, column_name)
                )""")
                matches: dict[str, list[str]] = {}
                for asset_id, relative_path in conn.execute("SELECT asset_id, relative_path FROM assets"):
                    match = re.match(r"(BJ[0-9]+(?:-[0-9]+)?)(?![A-Za-z0-9-])",
                                     Path(relative_path).stem, re.I)
                    if match:
                        matches.setdefault(match.group(1).upper(), []).append(asset_id)
                evidence = []
                for row_number, cells in rows:
                    identifier = cells.get("M", "").strip().upper()
                    if not re.fullmatch(r"BJ[0-9]+(?:-[0-9]+)?", identifier):
                        continue
                    for asset_id in matches.get(identifier, ()):
                        for column in ("E", "G", "H"):
                            if column in cells:
                                evidence.append((asset_id, "legacy_workbook", workbook_key,
                                                 str(workbook), row_number, column, cells[column]))
                conn.execute("DELETE FROM asset_descriptions "
                             "WHERE source = 'legacy_workbook' AND workbook_key = ?", (workbook_key,))
                conn.executemany("INSERT INTO asset_descriptions VALUES (?, ?, ?, ?, ?, ?, ?)", evidence)
        return len(evidence)

    def _database_uri(self) -> str:
        db_path = self.db_path.resolve()
        if db_path.is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        if not db_path.exists():
            raise FileNotFoundError(f"目录数据库不存在: {db_path}")
        db_uri = db_path.as_uri()
        if db_path.drive.startswith("\\\\"):
            # SQLite accepts UNC hosts in the path, not as URI authorities.
            db_uri = "file:////" + db_uri[len("file://"):]
        return db_uri

    def _validate_binding(self, conn: sqlite3.Connection) -> int:
        schema_version, stored_root = conn.execute(
            "SELECT schema_version, root_path FROM catalog_meta"
        ).fetchone()
        if stored_root != self.root_path:
            raise ValueError("目录数据库对应的素材库不一致")
        if schema_version not in (1, 2, SCHEMA_VERSION):
            raise ValueError(f"不支持目录数据库 schema 版本: {schema_version}")
        return schema_version

    def list_assets(self) -> list[Asset]:
        db_uri = self._database_uri()
        with closing(sqlite3.connect(f"{db_uri}?mode=ro", uri=True)) as conn:
            schema_version = self._validate_binding(conn)
            error_column = "probe_error" if schema_version >= 3 else "NULL"
            rows = conn.execute(f"""SELECT asset_id, relative_path, size_bytes, mtime_ns,
                probe_status, duration_seconds, width, height, frame_rate,
                video_codec, audio_codec, {error_column}
                FROM assets ORDER BY relative_path""").fetchall()
            descriptions: dict[str, list[AssetDescription]] = {}
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'asset_descriptions'").fetchone():
                for asset_id, source, workbook, row_number, column, text in conn.execute(
                    """SELECT asset_id, source, workbook_path, row_number, column_name, text
                        FROM asset_descriptions
                        ORDER BY workbook_path, row_number, column_name, text"""
                ):
                    descriptions.setdefault(asset_id, []).append(
                        AssetDescription(text, source, Path(workbook), row_number, column)
                    )
        return [Asset(row[0], self.library / Path(row[1]), Path(row[1]), *row[2:],
                      descriptions=tuple(descriptions.get(row[0], ())))
                for row in rows]

    def eligible_assets(self) -> list[Asset]:
        """Return only media confirmed playable by a completed probe."""
        return [asset for asset in self.list_assets()
                if asset.probe_status == "playable"
                and _valid_playable_cache(asset.duration_seconds, asset.width,
                                          asset.height, asset.probe_error, asset.frame_rate,
                                          asset.video_codec, asset.audio_codec,
                                          size_bytes=asset.size_bytes)]
