"""Catalog indexes media without changing the source library."""

import ntpath
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dub_align_studio.material_catalog import Catalog


class CatalogTests(unittest.TestCase):
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
