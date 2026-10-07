"""Catalog indexes media without changing the source library."""

import tempfile
import unittest
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
