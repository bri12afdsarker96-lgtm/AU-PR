"""Read-only media library inventory stored in a caller-owned SQLite cache."""

from __future__ import annotations

import hashlib
import ntpath
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .material_select import VIDEO_EXTENSIONS


SCHEMA_VERSION = 1


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


@dataclass(frozen=True)
class ScanStats:
    asset_count: int
    new_count: int
    changed_count: int
    unchanged_count: int


class Catalog:
    def __init__(self, db_path: Path, library: Path):
        self.library = Path(library).resolve()
        self.db_path = Path(db_path).resolve()
        if self.db_path.is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        self.root_path = ntpath.normcase(str(self.library))
        self.root_id = hashlib.sha256(self.root_path.encode("utf-8")).hexdigest()

    def refresh(self, probe: bool = True) -> ScanStats:
        if not self.library.is_dir():
            raise FileNotFoundError(self.library)
        assets = []
        for path in self.library.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            relative_path = path.relative_to(self.library)
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            asset_id = hashlib.sha256(
                f"{self.root_id}\0{ntpath.normcase(relative_path.as_posix())}".encode("utf-8")
            ).hexdigest()
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
                conn.execute("""CREATE TABLE IF NOT EXISTS catalog_meta (
                schema_version INTEGER NOT NULL,
                root_path TEXT NOT NULL
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
                audio_codec TEXT
            )""")
                conn.execute("DELETE FROM catalog_meta")
                conn.execute("INSERT INTO catalog_meta VALUES (?, ?)",
                             (SCHEMA_VERSION, self.root_path))
                existing = {
                    row[0]: row[1:]
                    for row in conn.execute(
                        "SELECT asset_id, relative_path, size_bytes, mtime_ns FROM assets"
                    )
                }
                new_count = changed_count = unchanged_count = 0
                for asset_id, relative_path, size_bytes, mtime_ns, probe_status in assets:
                    previous = existing.get(asset_id)
                    if previous is None:
                        conn.execute("""INSERT INTO assets (
                            asset_id, relative_path, size_bytes, mtime_ns, probe_status
                        ) VALUES (?, ?, ?, ?, ?)""",
                                     (asset_id, relative_path, size_bytes, mtime_ns,
                                      probe_status))
                        new_count += 1
                    elif previous[1:] == (size_bytes, mtime_ns):
                        if previous[0] != relative_path:
                            conn.execute("UPDATE assets SET relative_path = ? WHERE asset_id = ?",
                                         (relative_path, asset_id))
                        unchanged_count += 1
                    else:
                        conn.execute("""UPDATE assets SET relative_path = ?,
                            size_bytes = ?, mtime_ns = ?, probe_status = ?,
                            duration_seconds = NULL, width = NULL, height = NULL,
                            frame_rate = NULL, video_codec = NULL, audio_codec = NULL
                            WHERE asset_id = ?""",
                                     (relative_path, size_bytes, mtime_ns, probe_status,
                                      asset_id))
                        changed_count += 1
        return ScanStats(asset_count=len(assets), new_count=new_count,
                         changed_count=changed_count, unchanged_count=unchanged_count)

    def list_assets(self) -> list[Asset]:
        db_path = self.db_path.resolve()
        if db_path.is_relative_to(self.library):
            raise ValueError("目录数据库不能位于素材库内")
        if not db_path.exists():
            raise FileNotFoundError(f"目录数据库不存在: {db_path}")
        db_uri = db_path.as_uri()
        if db_path.drive.startswith("\\\\"):
            # SQLite accepts UNC hosts in the path, not as URI authorities.
            db_uri = "file:////" + db_uri[len("file://"):]
        with closing(sqlite3.connect(f"{db_uri}?mode=ro", uri=True)) as conn:
            stored_root = conn.execute("SELECT root_path FROM catalog_meta").fetchone()[0]
            if stored_root != self.root_path:
                raise ValueError("目录数据库对应的素材库不一致")
            rows = conn.execute("""SELECT asset_id, relative_path, size_bytes, mtime_ns,
                probe_status, duration_seconds, width, height, frame_rate,
                video_codec, audio_codec FROM assets ORDER BY relative_path""").fetchall()
        return [Asset(row[0], self.library / Path(row[1]), Path(row[1]), *row[2:])
                for row in rows]
