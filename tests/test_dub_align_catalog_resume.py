"""Resumable health checks use temporary media and preserve completed work."""

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dub_align_studio.material_catalog import Catalog, ProbeUnavailableError
from test_dub_align_legacy_metadata import _make_xlsx


def video_payload():
    return {"format": {"duration": "12.5"}, "streams": [
        {"codec_type": "video", "width": 1920, "height": 1080,
         "codec_name": "h264", "avg_frame_rate": "24/1"}]}


class CatalogResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalog_resume_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.sources = [self.library / name for name in ("a.mp4", "b.mp4")]
        for source in self.sources:
            source.write_bytes(b"temporary fixture")
        self.database = self.root / "cache.sqlite3"

    def test_cancel_after_first_commit_then_reopen_probes_only_remaining(self):
        calls, updates = [], []

        def probe(path):
            calls.append(path.name)
            return video_payload()

        catalog = Catalog(self.database, self.library, probe_func=probe)
        result = catalog.refresh_resumable(
            progress=updates.append, cancel_requested=lambda: bool(calls))
        self.assertEqual(calls, ["a.mp4"])
        self.assertEqual((result.total, result.processed, result.remaining), (2, 1, 1))
        self.assertEqual((result.playable, result.unreadable, result.pending), (1, 0, 1))
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(result.inventory_complete)
        self.assertEqual(updates[-1], result)
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()],
                         ["playable", "skipped"])

        resumed = Catalog(self.database, self.library, probe_func=probe).refresh_resumable()
        self.assertEqual(calls, ["a.mp4", "b.mp4"])
        self.assertEqual((resumed.total, resumed.processed, resumed.remaining), (2, 2, 0))
        self.assertEqual((resumed.playable, resumed.pending, resumed.state), (2, 0, "completed"))

    def test_file_changed_before_probe_stays_pending_for_next_run(self):
        calls = []

        def progress(update):
            if update.state == "running" and update.processed == 0:
                self.sources[0].write_bytes(b"new bytes after inventory")

        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: calls.append(path.name) or video_payload())
        result = catalog.refresh_resumable(progress=progress)
        self.assertEqual(calls, ["b.mp4"])
        first = catalog.list_assets()[0]
        self.assertEqual(first.probe_status, "pending")
        self.assertIn("变化", first.probe_error)
        self.assertIsNone(first.duration_seconds)
        self.assertEqual(first.size_bytes, self.sources[0].stat().st_size)
        self.assertEqual((result.remaining, result.pending, result.state), (0, 1, "incomplete"))
        catalog.refresh_resumable()
        self.assertEqual(calls, ["b.mp4", "a.mp4"])

    def test_file_changed_during_probe_discards_metadata_until_next_run(self):
        calls = []

        def probe(path):
            calls.append(path.name)
            if len(calls) == 1:
                path.write_bytes(b"changed while probing")
            return video_payload()

        catalog = Catalog(self.database, self.library, probe_func=probe)
        result = catalog.refresh_resumable()
        asset = catalog.list_assets()[0]
        self.assertEqual(asset.probe_status, "pending")
        self.assertIsNone(asset.duration_seconds)
        self.assertIn("变化", asset.probe_error)
        self.assertEqual((asset.size_bytes, asset.mtime_ns),
                         (self.sources[0].stat().st_size, self.sources[0].stat().st_mtime_ns))
        self.assertEqual((result.pending, result.playable, result.state), (1, 1, "incomplete"))
        catalog.refresh_resumable()
        self.assertEqual(calls, ["a.mp4", "b.mp4", "a.mp4"])

    def test_disappearance_before_or_during_probe_is_missing_without_stale_health(self):
        for timing in ("before", "during"):
            with self.subTest(timing=timing):
                self.sources[0].write_bytes(b"restored source")
                database = self.root / f"missing_{timing}.sqlite3"
                calls = []

                def progress(update):
                    if timing == "before" and update.state == "running" and update.processed == 0:
                        self.sources[0].unlink()

                def probe(path):
                    calls.append(path.name)
                    if timing == "during" and path == self.sources[0]:
                        path.unlink()
                    return video_payload()

                catalog = Catalog(database, self.library, probe_func=probe)
                result = catalog.refresh_resumable(progress=progress)
                self.assertEqual(calls, ["b.mp4"] if timing == "before" else ["a.mp4", "b.mp4"])
                asset = catalog.list_assets()[0]
                self.assertEqual(asset.probe_status, "missing")
                self.assertIn("消失", asset.probe_error)
                self.assertIsNone(asset.duration_seconds)
                self.assertEqual((result.playable, result.missing, result.pending), (1, 1, 0))

    def test_refresh_inside_probe_can_commit_without_old_result_overwriting_it(self):
        for health_refresh in (False, True):
            with self.subTest(health_refresh=health_refresh):
                database = self.root / f"concurrent_{health_refresh}.sqlite3"
                calls = []

                def newer_probe(path):
                    payload = video_payload()
                    payload["format"]["duration"] = "23"
                    return payload

                def probe(path):
                    calls.append(path.name)
                    if path.name == "a.mp4":
                        Catalog(database, self.library, probe_func=newer_probe).refresh(probe=health_refresh)
                    return video_payload()

                catalog = Catalog(database, self.library, probe_func=probe)
                result = catalog.refresh_resumable()
                first = catalog.list_assets()[0]
                self.assertEqual(first.probe_status, "playable" if health_refresh else "skipped")
                self.assertEqual(first.duration_seconds, 23 if health_refresh else None)
                self.assertEqual(calls, ["a.mp4"] if health_refresh else ["a.mp4", "b.mp4"])
                self.assertEqual(result.pending, 0 if health_refresh else 1)
                self.assertEqual(result.state, "completed" if health_refresh else "incomplete")

    def test_empty_source_is_unreadable_without_calling_probe(self):
        self.sources[0].write_bytes(b"")
        calls = []
        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: calls.append(path.name) or video_payload())
        result = catalog.refresh_resumable()
        self.assertEqual(calls, ["b.mp4"])
        asset = catalog.list_assets()[0]
        self.assertEqual(asset.probe_status, "unreadable")
        self.assertIn("空文件", asset.probe_error)
        self.assertIsNone(asset.duration_seconds)
        self.assertEqual((result.playable, result.unreadable, result.pending), (1, 1, 0))

    def test_cancellation_at_last_item_boundary_reports_zero_remaining(self):
        updates = []
        cancel = False

        def progress(update):
            nonlocal cancel
            updates.append(update)
            cancel = update.processed == 2

        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        result = catalog.refresh_resumable(progress=progress, cancel_requested=lambda: cancel)
        self.assertEqual(result.state, "cancelled")
        self.assertEqual((result.total, result.processed, result.remaining, result.playable), (2, 2, 0, 2))
        self.assertEqual(updates[-1], result)
        for update in updates:
            self.assertEqual(update.processed + update.remaining, update.total)
            self.assertEqual(update.playable + update.unreadable + update.missing + update.pending,
                             update.total)

    def test_terminal_counts_reflect_concurrent_invalidation_of_an_earlier_result(self):
        def probe(path):
            if path.name == "b.mp4":
                self.sources[0].write_bytes(b"replacement after first commit")
                Catalog(self.database, self.library).refresh(probe=False)
            return video_payload()

        catalog = Catalog(self.database, self.library, probe_func=probe)
        result = catalog.refresh_resumable()
        self.assertEqual((result.playable, result.pending, result.remaining), (0, 2, 0))
        self.assertEqual(result.state, "incomplete")
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()], ["skipped", "skipped"])

    def test_reporting_and_record_reads_scale_linearly_with_queue_size(self):
        for index in range(38):
            (self.library / f"extra_{index:03d}.mp4").write_bytes(b"fixture")
        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        updates = []
        with patch.object(catalog, "_probe_health", wraps=catalog._probe_health) as classify, \
                patch.object(catalog, "_probe_records", wraps=catalog._probe_records) as read:
            result = catalog.refresh_resumable(progress=updates.append)
        self.assertEqual((result.total, result.playable), (40, 40))
        self.assertGreaterEqual(len(updates), 40)
        self.assertLessEqual(classify.call_count, 8 * result.total)
        self.assertLessEqual(sum(not call.args for call in read.call_args_list), 2)
        self.assertLessEqual(read.call_count, 3 * result.total + 2)

    def test_tool_and_unexpected_errors_preserve_commits_without_poisoning_failed_item(self):
        for failure in (ProbeUnavailableError("ffprobe unavailable"), RuntimeError("unexpected bug")):
            with self.subTest(error=type(failure).__name__):
                database = self.root / f"error_{type(failure).__name__}.sqlite3"
                updates = []

                def probe(path):
                    if path.name == "b.mp4":
                        observer = Catalog(database, self.library)
                        self.assertEqual(observer.list_assets()[0].probe_status, "playable")
                        raise failure
                    return video_payload()

                catalog = Catalog(database, self.library, probe_func=probe)
                with self.assertRaisesRegex(type(failure), str(failure)):
                    catalog.refresh_resumable(progress=updates.append)
                self.assertEqual([asset.probe_status for asset in catalog.list_assets()],
                                 ["playable", "skipped"])
                self.assertEqual((updates[-1].processed, updates[-1].remaining), (1, 1))
                self.assertIsNone(catalog.list_assets()[1].probe_error)
                resumed_calls = []
                Catalog(database, self.library, probe_func=lambda path:
                        resumed_calls.append(path.name) or video_payload()).refresh_resumable()
                self.assertEqual(resumed_calls, ["b.mp4"])

    def test_progress_callback_exception_preserves_committed_result(self):
        def progress(update):
            if update.processed == 1:
                # A separate writer can acquire the database in a callback.
                with closing(sqlite3.connect(self.database, timeout=0)) as conn, conn:
                    conn.execute("UPDATE catalog_meta SET scan_generation = scan_generation")
                self.assertEqual(Catalog(self.database, self.library).list_assets()[0].probe_status,
                                 "playable")
                raise RuntimeError("progress callback failed")

        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        with self.assertRaisesRegex(RuntimeError, "progress callback failed"):
            catalog.refresh_resumable(progress=progress)
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()], ["playable", "skipped"])

    def test_cancel_after_inventory_preserves_full_inventory_and_missing_history(self):
        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: self.fail("cancelled probe ran"))
        catalog.refresh(probe=False)
        self.sources[0].unlink()
        updates = []
        result = catalog.refresh_resumable(progress=updates.append,
                                          cancel_requested=lambda: bool(updates))
        self.assertTrue(result.inventory_complete)
        self.assertEqual((result.total, result.processed, result.remaining, result.pending), (1, 0, 1, 1))
        self.assertEqual(result.state, "cancelled")
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()], ["missing", "skipped"])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT scan_generation FROM catalog_meta").fetchone(), (2,))

    def test_invalid_completed_cache_is_probed_again(self):
        for index, (status, duration, error) in enumerate((
            ("playable", None, None), ("playable", 12.5, "obsolete failure"),
            ("unreadable", None, None), ("unknown", None, None), ("pending", None, None),
        )):
            with self.subTest(status=status, duration=duration, error=error):
                database = self.root / f"invalid_{index}.sqlite3"
                calls = []
                catalog = Catalog(database, self.library,
                                  probe_func=lambda path: calls.append(path.name) or video_payload())
                catalog.refresh()
                calls.clear()
                with closing(sqlite3.connect(database)) as conn, conn:
                    conn.execute("UPDATE assets SET probe_status = ?, duration_seconds = ?, probe_error = ? "
                                 "WHERE relative_path = 'a.mp4'", (status, duration, error))
                result = catalog.refresh_resumable()
                self.assertEqual(calls, ["a.mp4"])
                self.assertEqual((result.playable, result.pending), (2, 0))

    def test_cache_changed_after_inventory_is_checked_again_before_reuse(self):
        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        catalog.refresh()
        calls = []

        def progress(update):
            if update.state == "running" and update.processed == 0:
                self.sources[0].write_bytes(b"changed completed cached media")

        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: calls.append(path.name) or video_payload())
        result = catalog.refresh_resumable(progress=progress)
        self.assertEqual(calls, [])
        self.assertEqual((result.playable, result.pending, result.state), (1, 1, "incomplete"))
        self.assertEqual(catalog.list_assets()[0].probe_status, "pending")

    def test_media_oserror_and_timeout_continue_and_default_timeout_is_bounded(self):
        for failure in (OSError("corrupt media"), subprocess.TimeoutExpired("ffprobe", 30)):
            with self.subTest(error=type(failure).__name__):
                database = self.root / f"media_{type(failure).__name__}.sqlite3"

                def probe(path):
                    if path.name == "a.mp4":
                        raise failure
                    return video_payload()

                result = Catalog(database, self.library, probe_func=probe).refresh_resumable()
                self.assertEqual((result.unreadable, result.playable), (1, 1))
        with patch("dub_align_studio.material_catalog.run_silent",
                   side_effect=subprocess.TimeoutExpired("ffprobe", 30)) as run:
            result = Catalog(self.database, self.library).refresh_resumable()
        self.assertEqual(result.unreadable, 2)
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [30, 30])

    def test_extension_column_does_not_break_or_participate_in_health_writeback(self):
        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        catalog.refresh(probe=False)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute('ALTER TABLE assets ADD COLUMN "note-text" TEXT')
            conn.execute('UPDATE assets SET "note-text" = ?', ("extension evidence",))
        result = catalog.refresh_resumable()
        self.assertEqual(result.playable, 2)
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute('SELECT "note-text" FROM assets').fetchall(),
                             [("extension evidence",), ("extension evidence",)])

    def test_changed_signature_that_returns_to_original_still_requires_a_probe(self):
        calls = []
        active = False
        source_stat = Path.stat
        seen = 0

        def progress(update):
            nonlocal active
            active = True

        def stat(path, *args, **kwargs):
            nonlocal seen
            original = source_stat(path, *args, **kwargs)
            if active and path == self.sources[0]:
                seen += 1
                if seen == 1:
                    return SimpleNamespace(st_size=original.st_size + 1, st_mtime_ns=original.st_mtime_ns)
            return original

        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: calls.append(path.name) or video_payload())
        with patch.object(Path, "stat", stat):
            result = catalog.refresh_resumable(progress=progress)
        self.assertEqual(calls, ["b.mp4"])
        first = catalog.list_assets()[0]
        self.assertEqual(first.probe_status, "pending")
        self.assertIsNone(first.duration_seconds)
        self.assertEqual((result.pending, result.state), (1, "incomplete"))

    def test_failed_inventory_leaves_generation_and_missing_status_unchanged(self):
        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: self.fail("probe ran after partial inventory"))
        catalog.refresh(probe=False)
        before = self.database.read_bytes()
        self.sources[1].unlink()

        def broken_walk(root):
            yield self.sources[0]
            raise PermissionError("blocked directory")

        with patch("dub_align_studio.material_catalog._iter_library_files", broken_walk):
            with self.assertRaisesRegex(PermissionError, "blocked directory"):
                catalog.refresh_resumable()
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()], ["skipped", "skipped"])
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT scan_generation FROM catalog_meta").fetchone(), (1,))

    def test_stat_permission_error_is_not_treated_as_missing_or_corrupt_media(self):
        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                database = self.root / f"permission_{timing}.sqlite3"
                active, calls = [], []
                source_stat = Path.stat

                def stat(path, *args, **kwargs):
                    if active and path == self.sources[1] and (timing == "before" or "b.mp4" in calls):
                        raise PermissionError("cannot stat source")
                    return source_stat(path, *args, **kwargs)

                catalog = Catalog(database, self.library,
                                  probe_func=lambda path: calls.append(path.name) or video_payload())
                with patch.object(Path, "stat", stat):
                    with self.assertRaisesRegex(PermissionError, "cannot stat source"):
                        catalog.refresh_resumable(progress=active.append)
                assets = catalog.list_assets()
                self.assertEqual([asset.probe_status for asset in assets], ["playable", "skipped"])
                self.assertIsNone(assets[1].probe_error)

    def test_missing_database_during_run_is_not_recreated(self):
        def progress(update):
            if update.processed == 1:
                self.database.rename(self.root / "saved.sqlite3")

        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        with self.assertRaisesRegex(FileNotFoundError, "目录数据库不存在"):
            catalog.refresh_resumable(progress=progress)
        self.assertFalse(self.database.exists())
        saved = Catalog(self.root / "saved.sqlite3", self.library).list_assets()
        self.assertEqual([asset.probe_status for asset in saved], ["playable", "skipped"])

    def test_redirected_database_is_rejected_before_each_health_write(self):
        redirected = False
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if redirected and path == self.database:
                return self.library / "forbidden.sqlite3"
            return original_resolve(path, *args, **kwargs)

        def probe(path):
            nonlocal redirected
            redirected = path.name == "b.mp4"
            return video_payload()

        catalog = Catalog(self.database, self.library, probe_func=probe)
        with patch.object(Path, "resolve", resolve):
            with self.assertRaisesRegex(ValueError, "数据库不能位于素材库内"):
                catalog.refresh_resumable()
        self.assertFalse((self.library / "forbidden.sqlite3").exists())
        self.assertEqual([asset.probe_status for asset in catalog.list_assets()], ["playable", "skipped"])

    def test_wrong_root_and_future_schema_are_rejected_without_database_changes(self):
        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        catalog.refresh(probe=False)
        other_root = self.root / "another_library"
        other_root.mkdir()
        (other_root / "other.mp4").write_bytes(b"fixture")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "素材库不一致"):
            Catalog(self.database, other_root).refresh_resumable()
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE catalog_meta SET schema_version = 999")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "schema.*999"):
            catalog.refresh_resumable()
        self.assertEqual(self.database.read_bytes(), before)

    def test_old_schemas_upgrade_in_inventory_before_resumable_probing(self):
        for version in (1, 2):
            with self.subTest(version=version):
                database = self.root / f"legacy_{version}.sqlite3"
                catalog = Catalog(database, self.library, probe_func=lambda path: video_payload())
                catalog.refresh(probe=False)
                with closing(sqlite3.connect(database)) as conn, conn:
                    conn.execute("ALTER TABLE assets DROP COLUMN probe_error")
                    if version == 1:
                        conn.execute("ALTER TABLE assets DROP COLUMN scan_generation")
                        conn.execute("ALTER TABLE catalog_meta DROP COLUMN scan_generation")
                    conn.execute("UPDATE catalog_meta SET schema_version = ?", (version,))
                result = catalog.refresh_resumable()
                self.assertEqual((result.playable, result.state), (2, "completed"))
                with closing(sqlite3.connect(database)) as conn:
                    self.assertEqual(conn.execute("SELECT schema_version FROM catalog_meta").fetchone(), (3,))

    def test_legacy_descriptions_and_all_source_bytes_survive_cancel_and_resume(self):
        self.sources[0].rename(self.library / "BJ1_地球.mp4")
        workbook = self.library / "legacy.xlsx"
        workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": " 地球\n", "G2": "星球", "H2": "备注"}))
        catalog = Catalog(self.database, self.library, probe_func=lambda path: video_payload())
        catalog.refresh(probe=False)
        self.assertEqual(catalog.import_legacy_metadata(workbook), 3)
        descriptions = {asset.asset_id: asset.descriptions for asset in catalog.list_assets()}
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.library.iterdir()}
        updates = []
        catalog.refresh_resumable(progress=updates.append,
                                  cancel_requested=lambda: bool(updates and updates[-1].processed == 1))
        Catalog(self.database, self.library, probe_func=lambda path: video_payload()).refresh_resumable()
        self.assertEqual({asset.asset_id: asset.descriptions for asset in catalog.list_assets()}, descriptions)
        self.assertEqual({path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.library.iterdir()}, before)

    def test_initial_cancel_reports_no_inventory_without_creating_database(self):
        updates = []
        catalog = Catalog(self.database, self.library,
                          probe_func=lambda path: self.fail("cancelled probe ran"))
        result = catalog.refresh_resumable(progress=updates.append, cancel_requested=lambda: True)
        self.assertFalse(self.database.exists())
        self.assertFalse(result.inventory_complete)
        self.assertEqual((result.total, result.processed, result.remaining), (0, 0, 0))
        self.assertEqual(result.state, "cancelled")
        self.assertTrue(result.reason)
        self.assertEqual(updates, [result])
        with self.assertRaises(FrozenInstanceError):
            result.state = "completed"

    def test_corrupt_media_is_committed_and_reused_without_stopping_other_probes(self):
        calls = []

        def probe(path):
            calls.append(path.name)
            if path.name == "a.mp4":
                raise ValueError("fixture is corrupt")
            with closing(sqlite3.connect(self.database)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT probe_status FROM assets WHERE relative_path = 'a.mp4'"
                ).fetchone(), ("unreadable",))
            return video_payload()

        catalog = Catalog(self.database, self.library, probe_func=probe)
        result = catalog.refresh_resumable()
        self.assertEqual((result.playable, result.unreadable, result.pending), (1, 1, 0))
        self.assertEqual(result.state, "completed")
        asset = catalog.list_assets()[0]
        self.assertIn("fixture is corrupt", asset.probe_error)
        self.assertIsNone(asset.duration_seconds)
        Catalog(self.database, self.library, probe_func=probe).refresh_resumable()
        self.assertEqual(calls, ["a.mp4", "b.mp4"])


if __name__ == "__main__":
    unittest.main()
