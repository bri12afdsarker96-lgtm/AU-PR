"""Catalog indexes media without changing the source library."""

import errno
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

            self.assertEqual(meta, (1, ntpath.normcase(str(library.resolve()))))
            self.assertGreaterEqual(columns, {"asset_id", "relative_path", "size_bytes",
                                              "mtime_ns", "probe_status", "duration_seconds",
                                              "width", "height", "frame_rate", "video_codec",
                                              "audio_codec"})
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
