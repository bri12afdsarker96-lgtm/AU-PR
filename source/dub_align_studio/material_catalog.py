"""Read-only media library inventory stored in a caller-owned SQLite cache."""

from __future__ import annotations

import hashlib
import ntpath
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .material_select import VIDEO_EXTENSIONS


SCHEMA_VERSION = 2
_EXCLUDED_FOLDERS = {"待人工复核", "非宇宙内容"}


def _raise_walk_error(error: OSError) -> None:
    raise error


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
        seen_asset_ids = {}
        for directory, _, filenames in os.walk(self.library, onerror=_raise_walk_error):
            for filename in filenames:
                path = Path(directory) / filename
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
                scan_generation INTEGER NOT NULL DEFAULT 0
            )""")
                stored_meta = conn.execute(
                    "SELECT schema_version, root_path FROM catalog_meta"
                ).fetchone()
                if stored_meta is not None and stored_meta[1] != self.root_path:
                    raise ValueError("目录数据库对应的素材库不一致")
                if stored_meta is not None and stored_meta[0] not in (1, SCHEMA_VERSION):
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
                            probe_status FROM assets"""
                    )
                }
                new_count = changed_count = unchanged_count = 0
                for asset_id, relative_path, size_bytes, mtime_ns, probe_status in assets:
                    previous = existing.get(asset_id)
                    if previous is None:
                        conn.execute("""INSERT INTO assets (
                            asset_id, relative_path, size_bytes, mtime_ns, probe_status,
                            scan_generation
                        ) VALUES (?, ?, ?, ?, ?, ?)""",
                                     (asset_id, relative_path, size_bytes, mtime_ns,
                                      probe_status, generation))
                        new_count += 1
                    elif previous[1:3] == (size_bytes, mtime_ns) and previous[3] != "missing":
                        conn.execute("""UPDATE assets SET relative_path = ?,
                            scan_generation = ? WHERE asset_id = ?""",
                                     (relative_path, generation, asset_id))
                        unchanged_count += 1
                    else:
                        conn.execute("""UPDATE assets SET relative_path = ?,
                            size_bytes = ?, mtime_ns = ?, probe_status = ?,
                            scan_generation = ?,
                            duration_seconds = NULL, width = NULL, height = NULL,
                            frame_rate = NULL, video_codec = NULL, audio_codec = NULL
                            WHERE asset_id = ?""",
                                     (relative_path, size_bytes, mtime_ns, probe_status,
                                      generation, asset_id))
                        changed_count += 1
                conn.execute("""UPDATE assets SET probe_status = 'missing'
                    WHERE scan_generation != ?""", (generation,))
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
            schema_version, stored_root = conn.execute(
                "SELECT schema_version, root_path FROM catalog_meta"
            ).fetchone()
            if stored_root != self.root_path:
                raise ValueError("目录数据库对应的素材库不一致")
            if schema_version not in (1, SCHEMA_VERSION):
                raise ValueError(f"不支持目录数据库 schema 版本: {schema_version}")
            rows = conn.execute("""SELECT asset_id, relative_path, size_bytes, mtime_ns,
                probe_status, duration_seconds, width, height, frame_rate,
                video_codec, audio_codec FROM assets ORDER BY relative_path""").fetchall()
        return [Asset(row[0], self.library / Path(row[1]), Path(row[1]), *row[2:])
                for row in rows]

    def eligible_assets(self) -> list[Asset]:
        """Return scanned assets that have not been marked missing."""
        return [asset for asset in self.list_assets()
                if asset.probe_status != "missing"]
