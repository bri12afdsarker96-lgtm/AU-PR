"""Catalog indexes media without changing the source library."""

import errno
import hashlib
import ntpath
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from dub_align_studio.material_catalog import Catalog


class CatalogTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "UNC URI behavior requires Windows")
    def test_list_assets_uses_read_only_uri_without_unc_authority(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"clip")
            database = root / "cache" / "catalog.sqlite3"
            catalog = Catalog(database, library)
            catalog.refresh(probe=False)
            unc_database = Path(r"\\server\share\catalog.sqlite3")
            original_resolve = Path.resolve
            original_exists = Path.exists
            original_connect = sqlite3.connect

            with self.assertRaisesRegex(sqlite3.OperationalError,
                                        "invalid uri authority: server"):
                original_connect(f"{unc_database.as_uri()}?mode=ro", uri=True)

            def resolve_unc(path, *args, **kwargs):
                if path == catalog.db_path:
                    return unc_database
                return original_resolve(path, *args, **kwargs)

            def exists_unc(path):
                if path == unc_database:
                    return True
                return original_exists(path)

            def connect_fixture(database_uri, *, uri=False):
                self.assertEqual(database_uri,
                                 "file:////server/share/catalog.sqlite3?mode=ro")
                self.assertTrue(uri)
                return original_connect(f"{database.as_uri()}?mode=ro", uri=True)

            with patch.object(Path, "resolve", resolve_unc), \
                    patch.object(Path, "exists", exists_unc), \
                    patch("dub_align_studio.material_catalog.sqlite3.connect",
                          side_effect=connect_fixture):
                assets = catalog.list_assets()

            self.assertEqual([asset.relative_path.name for asset in assets], ["clip.mp4"])

    def test_list_assets_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            cache = root / "cache"
            database = cache / "catalog.sqlite3"
            catalog = Catalog(database, library)

            with self.assertRaisesRegex(FileNotFoundError, "目录数据库不存在"):
                catalog.list_assets()

            self.assertFalse(database.exists())
            self.assertFalse(cache.exists())

    def test_catalog_database_lives_outside_library(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "BJ1_地球.mp4"
            source.write_bytes(b"original media bytes")
            before = source.stat()
            database = root / "cache" / "catalog.sqlite3"

            Catalog(database, library).refresh(probe=False)

            self.assertTrue(database.is_file())
            self.assertEqual([path for path in library.rglob("*") if path.is_file()], [source])
            self.assertEqual(source.read_bytes(), b"original media bytes")
            self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)
            assets = Catalog(database, library).list_assets()
            self.assertEqual(len(assets), 1)
            asset = assets[0]
            self.assertTrue(asset.asset_id)
            self.assertEqual(asset.path, source)
            self.assertEqual(asset.relative_path, Path("BJ1_地球.mp4"))
            self.assertEqual(asset.size_bytes, before.st_size)
            self.assertEqual(asset.mtime_ns, before.st_mtime_ns)

    def test_probe_false_records_skipped_status(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            catalog.refresh(probe=False)

            self.assertEqual(catalog.list_assets()[0].probe_status, "skipped")

    def test_refresh_preserves_unchanged_asset_and_cached_media(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            first = catalog.refresh()
            self.assertEqual((first.new_count, first.changed_count, first.unchanged_count),
                             (1, 0, 0))
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                with conn:
                    conn.execute("""UPDATE assets SET probe_status = 'ready',
                        duration_seconds = 12.5 WHERE relative_path = 'clip.mp4'""")

            second = catalog.refresh()

            self.assertEqual((second.new_count, second.changed_count,
                              second.unchanged_count), (0, 0, 1))
            asset = catalog.list_assets()[0]
            self.assertEqual(asset.probe_status, "ready")
            self.assertEqual(asset.duration_seconds, 12.5)

    def test_successful_refresh_advances_scan_generation_for_seen_assets(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            catalog.refresh(probe=False)
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                first_meta = conn.execute(
                    "SELECT scan_generation FROM catalog_meta"
                ).fetchone()[0]
                first_asset = conn.execute(
                    "SELECT scan_generation FROM assets"
                ).fetchone()[0]

            catalog.refresh(probe=False)
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                second_meta = conn.execute(
                    "SELECT scan_generation FROM catalog_meta"
                ).fetchone()[0]
                second_asset = conn.execute(
                    "SELECT scan_generation FROM assets"
                ).fetchone()[0]

            self.assertEqual((first_meta, first_asset), (1, 1))
            self.assertEqual((second_meta, second_asset), (2, 2))

    def test_renamed_asset_is_marked_missing_and_new_path_is_new_asset(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            original = library / "old.mp4"
            original.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)
            old_id = catalog.list_assets()[0].asset_id

            original.rename(library / "new.mp4")
            stats = catalog.refresh(probe=False)

            self.assertEqual((stats.asset_count, stats.new_count, stats.changed_count),
                             (1, 1, 0))
            rows = {asset.relative_path.name: asset for asset in catalog.list_assets()}
            self.assertEqual(rows["old.mp4"].asset_id, old_id)
            self.assertEqual(rows["old.mp4"].probe_status, "missing")
            self.assertNotEqual(rows["new.mp4"].asset_id, old_id)
            self.assertEqual(rows["new.mp4"].probe_status, "skipped")
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                generations = dict(conn.execute(
                    "SELECT relative_path, scan_generation FROM assets"
                ))
            self.assertEqual(generations, {"old.mp4": 1, "new.mp4": 2})

    def test_eligible_assets_excludes_missing_paths(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "old.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)
            source.rename(library / "new.mp4")
            catalog.refresh(probe=False)

            self.assertEqual([asset.relative_path.name for asset in catalog.eligible_assets()],
                             ["new.mp4"])
            self.assertEqual({asset.relative_path.name for asset in catalog.list_assets()},
                             {"old.mp4", "new.mp4"})

    def test_refresh_uses_storyboard_video_exclusions(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            for relative in (
                "valid.MP4", "notes.txt", "成片.mp4",
                "待人工复核/review.mp4", "非宇宙内容/other.mp4",
                "成片_segments/segment.mp4", "nested/custom_segments/segment.mp4",
                "nested/valid.mov",
            ):
                path = library / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            stats = catalog.refresh(probe=False)

            self.assertEqual(stats.asset_count, 2)
            self.assertEqual([asset.relative_path.as_posix()
                              for asset in catalog.eligible_assets()],
                             ["nested/valid.mov", "valid.MP4"])

    def test_failed_scan_does_not_mark_unseen_assets_missing(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            first = library / "first.mp4"
            second = library / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)

            def broken_walk(_self, _pattern):
                yield first
                raise PermissionError("incomplete scan")

            with patch.object(Path, "rglob", broken_walk):
                with self.assertRaisesRegex(PermissionError, "incomplete scan"):
                    catalog.refresh(probe=False)

            with closing(sqlite3.connect(catalog.db_path)) as conn:
                generation = conn.execute(
                    "SELECT scan_generation FROM catalog_meta"
                ).fetchone()[0]
            self.assertEqual(generation, 1)
            self.assertEqual({asset.relative_path.name: asset.probe_status
                              for asset in catalog.list_assets()},
                             {"first.mp4": "skipped", "second.mp4": "skipped"})

    def test_stat_permission_error_does_not_mark_assets_missing(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "clip.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)
            original_stat = Path.stat

            def denied_stat(path, *args, **kwargs):
                if path == source:
                    raise PermissionError("cannot inspect media")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", denied_stat):
                with self.assertRaisesRegex(PermissionError, "cannot inspect media"):
                    catalog.refresh(probe=False)

            self.assertEqual(catalog.list_assets()[0].probe_status, "skipped")

    def test_refresh_upgrades_v1_database_without_losing_probe_cache(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "clip.mp4"
            source.write_bytes(b"clip")
            database = root / "cache" / "catalog.sqlite3"
            database.parent.mkdir()
            catalog = Catalog(database, library)
            asset_id = hashlib.sha256(
                f"{catalog.root_id}\0clip.mp4".encode("utf-8")
            ).hexdigest()
            with closing(sqlite3.connect(database)) as conn:
                with conn:
                    conn.execute("""CREATE TABLE catalog_meta (
                        schema_version INTEGER NOT NULL, root_path TEXT NOT NULL)""")
                    conn.execute("INSERT INTO catalog_meta VALUES (1, ?)",
                                 (catalog.root_path,))
                    conn.execute("""CREATE TABLE assets (
                        asset_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                        size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                        probe_status TEXT NOT NULL, duration_seconds REAL,
                        width INTEGER, height INTEGER, frame_rate REAL,
                        video_codec TEXT, audio_codec TEXT)""")
                    conn.execute("""INSERT INTO assets VALUES
                        (?, 'clip.mp4', ?, ?, 'ready', 12.5, 1920, 1080, 30.0,
                         'h264', 'aac')""",
                                 (asset_id, source.stat().st_size, source.stat().st_mtime_ns))

            stats = catalog.refresh()

            self.assertEqual((stats.new_count, stats.changed_count,
                              stats.unchanged_count), (0, 0, 1))
            asset = catalog.list_assets()[0]
            self.assertEqual((asset.probe_status, asset.duration_seconds,
                              asset.video_codec), ("ready", 12.5, "h264"))
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT schema_version, scan_generation FROM catalog_meta"
                ).fetchone(), (2, 1))
                self.assertEqual(conn.execute(
                    "SELECT scan_generation FROM assets"
                ).fetchone(), (1,))

    def test_refresh_rejects_unknown_schema_without_rewriting_database(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                with conn:
                    conn.execute("UPDATE catalog_meta SET schema_version = 99")
            before = catalog.db_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "schema.*99"):
                catalog.refresh(probe=False)

            self.assertEqual(catalog.db_path.read_bytes(), before)

    def test_removed_asset_is_marked_missing_while_unchanged_media_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            removed = library / "removed.mp4"
            removed.write_bytes(b"old")
            (library / "kept.mp4").write_bytes(b"keep")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh()
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                with conn:
                    conn.execute("""UPDATE assets SET probe_status = 'ready',
                        duration_seconds = 12.5""")

            removed.unlink()
            stats = catalog.refresh()

            self.assertEqual((stats.asset_count, stats.new_count, stats.changed_count,
                              stats.unchanged_count), (1, 0, 0, 1))
            rows = {asset.relative_path.name: asset for asset in catalog.list_assets()}
            self.assertEqual(rows["removed.mp4"].probe_status, "missing")
            self.assertEqual(rows["kept.mp4"].probe_status, "ready")
            self.assertEqual(rows["kept.mp4"].duration_seconds, 12.5)

    def test_returned_missing_asset_is_reactivated_and_reprobed(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "clip.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh()
            original_id = catalog.list_assets()[0].asset_id
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                with conn:
                    conn.execute("""UPDATE assets SET probe_status = 'ready',
                        duration_seconds = 12.5""")
            original_bytes = source.read_bytes()
            original_mtime = source.stat().st_mtime_ns

            source.unlink()
            catalog.refresh()
            source.write_bytes(original_bytes)
            os.utime(source, ns=(original_mtime, original_mtime))
            stats = catalog.refresh()

            self.assertEqual((stats.new_count, stats.changed_count,
                              stats.unchanged_count), (0, 1, 0))
            asset = catalog.list_assets()[0]
            self.assertEqual(asset.asset_id, original_id)
            self.assertEqual(asset.probe_status, "pending")
            self.assertIsNone(asset.duration_seconds)

    def test_refresh_marks_changed_asset_for_reprobe(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "clip.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh()
            with closing(sqlite3.connect(catalog.db_path)) as conn:
                with conn:
                    conn.execute("""UPDATE assets SET probe_status = 'ready',
                        duration_seconds = 12.5 WHERE relative_path = 'clip.mp4'""")

            old_mtime_ns = source.stat().st_mtime_ns
            source.write_bytes(b"updated clip")
            os.utime(source, ns=(old_mtime_ns + 2_000_000_000,
                                 old_mtime_ns + 2_000_000_000))
            stats = catalog.refresh()

            self.assertEqual((stats.new_count, stats.changed_count,
                              stats.unchanged_count), (0, 1, 0))
            asset = catalog.list_assets()[0]
            self.assertEqual(asset.size_bytes, len(b"updated clip"))
            self.assertEqual(asset.mtime_ns, source.stat().st_mtime_ns)
            self.assertEqual(asset.probe_status, "pending")
            self.assertIsNone(asset.duration_seconds)

    def test_asset_id_survives_relative_path_case_change(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "BJ1_地球.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)
            original_id = catalog.list_assets()[0].asset_id

            source.rename(library / "bj1_地球.mp4")
            catalog.refresh(probe=False)

            self.assertEqual(catalog.list_assets()[0].relative_path.name, "bj1_地球.mp4")
            self.assertEqual(catalog.list_assets()[0].asset_id, original_id)

    def test_refresh_rejects_case_colliding_paths_before_database_write(self):
        for incremental in (False, True):
            with self.subTest(incremental=incremental), \
                    tempfile.TemporaryDirectory(prefix="catalog_") as temp:
                root = Path(temp)
                library = root / "library"
                library.mkdir()
                upper = library / "A.mp4"
                lower = library / "a.mp4"
                upper.write_bytes(b"clip")
                database = root / "cache" / "catalog.sqlite3"
                catalog = Catalog(database, library)
                if incremental:
                    catalog.refresh(probe=False)
                before = database.read_bytes() if incremental else None
                original_is_file = Path.is_file
                original_stat = Path.stat

                def is_file_with_collision(path, *args, **kwargs):
                    if str(path) == str(lower):
                        return True
                    return original_is_file(path, *args, **kwargs)

                def stat_with_collision(path, *args, **kwargs):
                    if str(path) == str(lower):
                        return original_stat(upper, *args, **kwargs)
                    return original_stat(path, *args, **kwargs)

                with patch.object(Path, "rglob", return_value=[upper, lower]), \
                        patch.object(Path, "is_file", is_file_with_collision), \
                        patch.object(Path, "stat", stat_with_collision):
                    with self.assertRaisesRegex(ValueError, "路径.*冲突"):
                        catalog.refresh(probe=False)

                if incremental:
                    self.assertEqual(database.read_bytes(), before)
                    self.assertEqual([asset.relative_path.name
                                      for asset in catalog.list_assets()], ["A.mp4"])
                else:
                    self.assertFalse(database.exists())

    def test_root_path_uses_windows_case_normalization(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "Straße"
            library.mkdir()
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            catalog.refresh(probe=False)

            with closing(sqlite3.connect(catalog.db_path)) as conn:
                stored_root = conn.execute("SELECT root_path FROM catalog_meta").fetchone()[0]
            self.assertEqual(stored_root, ntpath.normcase(str(library.resolve())))

    def test_database_path_inside_library_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            library = Path(temp) / "library"
            library.mkdir()
            database = library / "cache" / "catalog.sqlite3"

            with self.assertRaises(ValueError):
                Catalog(database, library)

            self.assertFalse(database.exists())

    def test_list_assets_rejects_database_from_another_library(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            first_library = root / "first"
            second_library = root / "second"
            first_library.mkdir()
            second_library.mkdir()
            (first_library / "clip.mp4").write_bytes(b"first")
            database = root / "cache" / "catalog.sqlite3"
            Catalog(database, first_library).refresh(probe=False)

            with self.assertRaises(ValueError):
                Catalog(database, second_library).list_assets()

    def test_refresh_rejects_database_from_another_library_without_changes(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            first_library = root / "first"
            second_library = root / "second"
            first_library.mkdir()
            second_library.mkdir()
            (first_library / "first.mp4").write_bytes(b"first")
            (second_library / "second.mp4").write_bytes(b"second")
            database = root / "cache" / "catalog.sqlite3"
            first_catalog = Catalog(database, first_library)
            first_catalog.refresh(probe=False)

            with closing(sqlite3.connect(database)) as conn:
                before_meta = conn.execute("SELECT * FROM catalog_meta").fetchall()
                before_assets = conn.execute("SELECT * FROM assets").fetchall()

            with self.assertRaisesRegex(ValueError, "素材库不一致"):
                Catalog(database, second_library).refresh(probe=False)

            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute("SELECT * FROM catalog_meta").fetchall(),
                                 before_meta)
                self.assertEqual(conn.execute("SELECT * FROM assets").fetchall(),
                                 before_assets)
            self.assertEqual([asset.relative_path.name
                              for asset in first_catalog.list_assets()], ["first.mp4"])

    def test_refresh_skips_video_removed_before_stat(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            vanished = library / "a.mp4"
            vanished.write_bytes(b"vanished")
            (library / "b.mp4").write_bytes(b"survivor")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            original_stat = Path.stat
            vanished_stat_calls = 0

            def stat_with_disappearance(path, *args, **kwargs):
                nonlocal vanished_stat_calls
                if path == vanished:
                    vanished_stat_calls += 1
                    if vanished_stat_calls == 2:
                        raise FileNotFoundError(path)
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", stat_with_disappearance):
                stats = catalog.refresh(probe=False)

            self.assertGreaterEqual(vanished_stat_calls, 2)
            self.assertEqual(stats.asset_count, 1)
            self.assertEqual([asset.relative_path.name for asset in catalog.list_assets()],
                             ["b.mp4"])

    def test_refresh_rejects_cache_redirected_into_library_after_construction(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            inside_cache = library / "cache"
            inside_cache.mkdir()
            database = root / "cache" / "catalog.sqlite3"
            catalog = Catalog(database, library)
            original_resolve = Path.resolve

            def resolve_after_redirect(path, *args, **kwargs):
                if path == catalog.db_path:
                    return inside_cache / database.name
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve_after_redirect):
                with self.assertRaises(ValueError):
                    catalog.refresh(probe=False)

            self.assertFalse(database.exists())
            self.assertFalse((inside_cache / database.name).exists())

    def test_list_assets_rejects_cache_redirected_into_library_after_construction(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            database = root / "cache" / "catalog.sqlite3"
            catalog = Catalog(database, library)
            catalog.refresh(probe=False)
            original_resolve = Path.resolve

            def resolve_after_redirect(path, *args, **kwargs):
                if path == catalog.db_path:
                    return library / "cache" / database.name
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve_after_redirect):
                with self.assertRaisesRegex(ValueError, "目录数据库不能位于素材库内"):
                    catalog.list_assets()

    def test_list_assets_rejects_redirected_cache(self):
        self._assert_list_assets_rejects_redirected_cache()

    def test_list_assets_rejects_redirected_cache_with_ampersand_path(self):
        self._assert_list_assets_rejects_redirected_cache("amp&redirect")

    def _assert_list_assets_rejects_redirected_cache(self, subdirectory=None):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            if subdirectory is not None:
                root /= subdirectory
                root.mkdir()
            library = root / "library"
            library.mkdir()
            (library / "clip.mp4").write_bytes(b"original media bytes")
            cache = root / "cache"
            database = cache / "catalog.sqlite3"
            catalog = Catalog(database, library)
            catalog.refresh(probe=False)

            inside_cache = library / "cache"
            inside_cache.mkdir()
            cache.rename(root / "former_cache")
            if os.name == "nt":
                result = subprocess.run(
                    [
                        "powershell", "-NoProfile", "-NonInteractive", "-Command",
                        "try { New-Item -ItemType Junction "
                        "-Path $env:CATALOG_JUNCTION_LINK "
                        "-Target $env:CATALOG_JUNCTION_TARGET "
                        "-ErrorAction Stop | Out-Null } "
                        "catch { "
                        "if ($_.CategoryInfo.Category -eq 'PermissionDenied' -or "
                        "$_.Exception -is [System.UnauthorizedAccessException] -or "
                        "$_.Exception -is [System.PlatformNotSupportedException]) { exit 77 }; "
                        "[Console]::Error.WriteLine($_.Exception.ToString()); exit 1 }",
                    ],
                    capture_output=True, text=True,
                    env={
                        **os.environ,
                        "CATALOG_JUNCTION_LINK": str(cache),
                        "CATALOG_JUNCTION_TARGET": str(inside_cache),
                    },
                )
                if result.returncode == 77:
                    self.skipTest(f"cannot create directory junction: {result.stderr}")
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self._create_directory_symlink(inside_cache, cache)
            self.assertEqual(database.resolve(), inside_cache / database.name)

            def library_snapshot():
                return {
                    path.relative_to(library).as_posix():
                        ("directory", None) if path.is_dir() else ("file", path.read_bytes())
                    for path in library.rglob("*")
                }

            before = library_snapshot()
            with self.assertRaisesRegex(ValueError, "目录数据库不能位于素材库内"):
                catalog.list_assets()
            self.assertEqual(library_snapshot(), before)
            self.assertFalse((inside_cache / database.name).exists())

    def _create_directory_symlink(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            if exc.errno in {
                errno.EPERM, errno.EACCES, errno.ENOSYS,
                errno.ENOTSUP, errno.EOPNOTSUPP,
            }:
                self.skipTest(f"cannot create directory symlink: {exc}")
            raise

    def test_directory_symlink_invalid_path_error_is_not_skipped(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            with patch("os.symlink", side_effect=OSError(errno.EINVAL, "invalid path")):
                with self.assertRaisesRegex(OSError, "invalid path"):
                    try:
                        self._create_directory_symlink(root / "target", root / "link")
                    except unittest.SkipTest as exc:
                        self.fail(f"path handling error was skipped: {exc}")

    def test_directory_symlink_permission_or_unsupported_errors_are_skipped(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            for error_code in (
                errno.EPERM, errno.EACCES, errno.ENOSYS,
                errno.ENOTSUP, errno.EOPNOTSUPP,
            ):
                with self.subTest(error_code=error_code):
                    with patch("os.symlink", side_effect=OSError(error_code, "unavailable")):
                        with self.assertRaises(unittest.SkipTest):
                            self._create_directory_symlink(root / "target", root / "link")

    def test_refresh_rejects_redirect_before_creating_missing_cache_directory(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            cache_parent = root / "cache"
            cache_parent.mkdir()
            database = cache_parent / "missing" / "catalog.sqlite3"
            catalog = Catalog(database, library)
            original_resolve = Path.resolve

            def resolve_after_redirect(path, *args, **kwargs):
                if path == catalog.db_path:
                    return library / "missing" / database.name
                return original_resolve(path, *args, **kwargs)

            with patch.object(Path, "resolve", resolve_after_redirect), \
                    patch.object(Path, "mkdir") as mkdir:
                with self.assertRaises(ValueError):
                    catalog.refresh(probe=False)
                mkdir.assert_not_called()

            self.assertFalse((library / "missing").exists())

    def test_schema_and_media_fields_are_persisted(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            source = library / "BJ1_地球.mp4"
            source.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)
            catalog.refresh(probe=False)

            with closing(sqlite3.connect(catalog.db_path)) as conn:
                meta = conn.execute("SELECT schema_version, root_path FROM catalog_meta").fetchone()
                columns = {row[1] for row in conn.execute("PRAGMA table_info(assets)")}
                row = conn.execute("""SELECT relative_path, size_bytes, mtime_ns,
                    probe_status, duration_seconds, width, height, frame_rate,
                    video_codec, audio_codec FROM assets""").fetchone()

            self.assertEqual(meta, (2, ntpath.normcase(str(library.resolve()))))
            self.assertGreaterEqual(columns, {"asset_id", "relative_path", "size_bytes",
                                              "mtime_ns", "probe_status", "duration_seconds",
                                              "width", "height", "frame_rate", "video_codec",
                                              "audio_codec", "scan_generation"})
            self.assertEqual(row[:4], ("BJ1_地球.mp4", source.stat().st_size,
                                       source.stat().st_mtime_ns, "skipped"))
            self.assertEqual(row[4:], (None, None, None, None, None, None))

    def test_same_bj_number_in_different_paths_has_distinct_stable_ids(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            for folder in ("chapter_a", "chapter_b"):
                target = library / folder / "BJ1_地球.mp4"
                target.parent.mkdir(parents=True)
                target.write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            catalog.refresh(probe=False)
            first = {asset.relative_path.as_posix(): asset.asset_id
                     for asset in catalog.list_assets()}
            catalog.refresh(probe=False)
            second = {asset.relative_path.as_posix(): asset.asset_id
                      for asset in catalog.list_assets()}

            self.assertEqual(len(first), 2)
            self.assertEqual(len(set(first.values())), 2)
            self.assertEqual(second, first)

    def test_unicode_names_that_windows_distinguishes_have_distinct_ids(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            library = root / "library"
            library.mkdir()
            for name in ("Straße.mp4", "Strasse.mp4"):
                (library / name).write_bytes(b"clip")
            catalog = Catalog(root / "cache" / "catalog.sqlite3", library)

            catalog.refresh(probe=False)
            assets = catalog.list_assets()

            self.assertEqual({asset.relative_path.name for asset in assets},
                             {"Straße.mp4", "Strasse.mp4"})
            self.assertEqual(len({asset.asset_id for asset in assets}), 2)

    def test_distinct_unicode_library_roots_have_distinct_ids(self):
        with tempfile.TemporaryDirectory(prefix="catalog_") as temp:
            root = Path(temp)
            catalogs = []
            for name in ("Straße", "Strasse"):
                library = root / name
                library.mkdir()
                (library / "clip.mp4").write_bytes(b"clip")
                catalog = Catalog(root / "cache" / f"{name}.sqlite3", library)
                catalog.refresh(probe=False)
                catalogs.append(catalog)

            self.assertNotEqual(catalogs[0].root_id, catalogs[1].root_id)
            self.assertNotEqual(catalogs[0].list_assets()[0].asset_id,
                                catalogs[1].list_assets()[0].asset_id)
