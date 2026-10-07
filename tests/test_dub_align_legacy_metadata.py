"""Optional workbook evidence enriches assets without changing the media library."""

import hashlib
import io
import ntpath
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from dub_align_studio import xlsx_reader
from dub_align_studio.material_catalog import Catalog
from dub_align_studio.storyboard import read_storyboard


def _make_xlsx(cells: dict[str, str], *, shared_strings: bool = False) -> bytes:
    rows = {}
    shared = []
    for ref, text in cells.items():
        row = int("".join(char for char in ref if char.isdigit()))
        if shared_strings:
            cell = f'<c r="{ref}" t="s"><v>{len(shared)}</v></c>'
            shared.append(f'<si><t xml:space="preserve">{escape(text)}</t></si>')
        else:
            cell = f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
        rows.setdefault(row, []).append(cell)
    sheet = "".join(f'<row r="{row}">{"".join(values)}</row>'
                    for row, values in rows.items())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        if shared_strings:
            bundle.writestr(
                "xl/sharedStrings.xml",
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'{"".join(shared)}</sst>',
            )
        bundle.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{sheet}</sheetData></worksheet>",
        )
    return buffer.getvalue()


class WorkbookRowsTests(unittest.TestCase):
    def test_raw_columns_preserve_text_while_default_readers_still_trim(self):
        for shared_strings in (False, True):
            with self.subTest(shared_strings=shared_strings):
                workbook = _make_xlsx({"E2": " \n地球\t ", "E3": " \n\t", "M2": " bj1 "},
                                      shared_strings=shared_strings)
                self.assertEqual(xlsx_reader.read_columns(workbook, ("E", "M"), strip=False),
                                 [(2, {"E": " \n地球\t ", "M": " bj1 "})])
                self.assertEqual(xlsx_reader.read_columns(workbook, ("E", "M")),
                                 [(2, {"E": "地球", "M": "bj1"})])
                self.assertEqual(xlsx_reader.read_column(workbook, "E"), ["地球"])

    def test_storyboard_keeps_trimming_shared_and_inline_text(self):
        headers = ("镜头序号", "口播文稿", "时长", "图片生成提示词", "视频生成提示词")
        cells = {f"{column}1": f" \n{title}\t " for column, title in zip("ABCDE", headers)}
        cells.update({"A2": " 1\n", "B2": "\n地球  ", "C2": " 3秒\n",
                      "D2": " 图像\t", "E2": "\n运动 "})
        with tempfile.TemporaryDirectory(prefix="legacy_storyboard_") as directory:
            path = Path(directory) / "storyboard.xlsx"
            for shared_strings in (False, True):
                with self.subTest(shared_strings=shared_strings):
                    path.write_bytes(_make_xlsx(cells, shared_strings=shared_strings))
                    shot = read_storyboard(path).shots[0]
                    self.assertEqual((shot.label, shot.narration, shot.duration,
                                      shot.image_prompt, shot.video_prompt),
                                     ("1", "地球", 3.0, "图像", "运动"))

    def test_selected_columns_keep_actual_rows_and_cell_holes(self):
        workbook = _make_xlsx({
            "M9": "BJ9", "G9": "星系", "E2": "地球", "M2": "BJ2",
            "H4": "无编号描述", "E9": "   ", "B6": "无关列",
        })
        self.assertEqual(xlsx_reader.read_columns(workbook, ("m", "E", "G", "H")), [
            (2, {"E": "地球", "M": "BJ2"}),
            (4, {"H": "无编号描述"}),
            (9, {"M": "BJ9", "G": "星系"}),
        ])
        self.assertEqual(xlsx_reader.read_column(workbook, "E"), ["地球"])

    def test_first_worksheet_follows_workbook_order_instead_of_zip_filename(self):
        buffer = io.BytesIO(_make_xlsx({"M2": "BJ1", "E2": "错误工作表"}))
        with zipfile.ZipFile(buffer, "a") as bundle:
            bundle.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="第一张" sheetId="2" r:id="rId2"/>'
                '<sheet name="第二张" sheetId="1" r:id="rId1"/></sheets></workbook>',
            )
            bundle.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                '</Relationships>',
            )
            with zipfile.ZipFile(io.BytesIO(_make_xlsx({"M7": "BJ2", "E7": "实际第一张"}))) as first:
                bundle.writestr("xl/worksheets/sheet2.xml", first.read("xl/worksheets/sheet1.xml"))
        self.assertEqual(xlsx_reader.read_columns(buffer.getvalue(), ("M", "E")),
                         [(7, {"M": "BJ2", "E": "实际第一张"})])


class LegacyMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="legacy_metadata_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.database = self.root / "cache" / "catalog.sqlite3"
        self.catalog = Catalog(self.database, self.library)
        self.workbook = self.root / "legacy.xlsx"

    def add_video(self, relative_path):
        path = self.library / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture video")
        return path

    def test_workbook_descriptions_bind_to_each_matching_asset_by_real_row(self):
        for relative_path in ("a/BJ1_地球.mp4", "b/bj1_地球.mov", "BJ9_星系.mp4",
                              "unlisted.mp4", "BJ3_无有效描述.mp4"):
            self.add_video(relative_path)
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({
            "M2": "BJ1", "E2": "地球 <蓝色>", "G2": "行星", "H2": "太空 & 轨道",
            "E4": "没有编号，不可错配", "M6": "   ", "H6": "空编号不绑定",
            "M7": "随笔 BJ3", "G7": "无效编号不绑定",
            "M9": "bj9", "G9": "遥远星系", "M12": "BJ99", "E12": "库中不存在",
        }))

        count = self.catalog.import_legacy_metadata(self.workbook)

        assets = self.catalog.list_assets()
        self.assertEqual(count, 7)
        earth = [asset for asset in assets if asset.relative_path.name.lower().startswith("bj1_")]
        self.assertEqual(len({asset.asset_id for asset in earth}), 2)
        self.assertEqual(earth[0].descriptions, earth[1].descriptions)
        self.assertEqual([(item.text, item.source, item.workbook_path, item.row_number, item.column)
                          for item in earth[0].descriptions], [
            ("地球 <蓝色>", "legacy_workbook", self.workbook.resolve(), 2, "E"),
            ("行星", "legacy_workbook", self.workbook.resolve(), 2, "G"),
            ("太空 & 轨道", "legacy_workbook", self.workbook.resolve(), 2, "H"),
        ])
        by_name = {asset.relative_path.name: asset for asset in assets}
        self.assertEqual([(item.text, item.row_number, item.column)
                          for item in by_name["BJ9_星系.mp4"].descriptions],
                         [("遥远星系", 9, "G")])
        self.assertEqual(by_name["unlisted.mp4"].descriptions, ())
        self.assertEqual(by_name["BJ3_无有效描述.mp4"].descriptions, ())

    def test_reimport_is_idempotent_but_distinct_rows_and_columns_remain_evidence(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({
            "M2": "BJ1", "E2": "地球", "M5": "BJ1", "E5": "地球", "G5": "轨道",
        }))
        self.assertEqual(self.catalog.import_legacy_metadata(self.workbook), 3)
        before = self.catalog.list_assets()[0].descriptions
        self.assertEqual([(item.text, item.row_number, item.column) for item in before],
                         [("地球", 2, "E"), ("地球", 5, "E"), ("轨道", 5, "G")])

        self.assertEqual(self.catalog.import_legacy_metadata(self.workbook), 3)

        self.assertEqual(self.catalog.list_assets()[0].descriptions, before)

    def test_descriptions_preserve_nonblank_original_text(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        original = {"E": "  地球 <蓝色>\n", "G": "\t行星  ", "H": "\n太空\n轨道\n "}
        for shared_strings in (False, True):
            with self.subTest(shared_strings=shared_strings):
                self.workbook.write_bytes(_make_xlsx({
                    "M2": "\n bj1 \t", **{f"{column}2": text for column, text in original.items()},
                    "M3": "BJ1", "E3": " \n\t", "G3": "", "H3": "\u3000",
                }, shared_strings=shared_strings))

                self.assertEqual(self.catalog.import_legacy_metadata(self.workbook), 3)

                self.assertEqual([(item.column, item.text, item.row_number)
                                  for item in self.catalog.list_assets()[0].descriptions],
                                 [(column, text, 2) for column, text in original.items()])

    def test_updated_workbook_replaces_only_its_own_evidence(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "旧描述"}))
        other = self.root / "other.xlsx"
        other.write_bytes(_make_xlsx({"M4": "BJ1", "H4": "第二份证据"}))
        self.catalog.import_legacy_metadata(self.workbook)
        self.catalog.import_legacy_metadata(other)
        self.workbook.write_bytes(_make_xlsx({"M7": "BJ1", "G7": "新描述"}))

        self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual([(item.text, item.workbook_path, item.row_number, item.column)
                          for item in self.catalog.list_assets()[0].descriptions],
                         [("新描述", self.workbook, 7, "G"), ("第二份证据", other, 4, "H")])
        self.workbook.write_bytes(_make_xlsx({"M2": "无效", "E2": "不绑定"}))
        self.assertEqual(self.catalog.import_legacy_metadata(self.workbook), 0)
        self.assertEqual([item.text for item in self.catalog.list_assets()[0].descriptions],
                         ["第二份证据"])

    def test_failed_replacement_rolls_back_all_of_that_workbooks_evidence(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "原始证据"}))
        self.catalog.import_legacy_metadata(self.workbook)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_evidence BEFORE INSERT ON asset_descriptions "
                         "WHEN NEW.text = 'rejected' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        before = self.database.read_bytes()
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "accepted", "G2": "rejected"}))

        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture failure"):
            self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual([item.text for item in self.catalog.list_assets()[0].descriptions],
                         ["原始证据"])

    def test_missing_workbook_is_optional_and_does_not_change_or_create_cache(self):
        self.add_video("BJ1_地球.mp4")
        with self.assertRaises(FileNotFoundError):
            self.catalog.import_legacy_metadata(self.workbook)
        self.assertFalse(self.database.parent.exists())
        self.catalog.refresh(probe=False)
        before = self.database.read_bytes()

        with self.assertRaises(FileNotFoundError):
            self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.catalog.list_assets()[0].descriptions, ())
        self.assertEqual(self.catalog.refresh(probe=False).unchanged_count, 1)

    def test_invalid_workbook_does_not_replace_existing_evidence(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "原始证据"}))
        self.catalog.import_legacy_metadata(self.workbook)
        before = self.database.read_bytes()
        self.workbook.write_bytes(b"not a workbook")

        with self.assertRaises(ValueError):
            self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual(self.database.read_bytes(), before)

    def test_import_preserves_health_signatures_generation_and_source_files(self):
        self.add_video("BJ1_地球.mp4")
        workbook = self.library / "参考.xlsx"
        workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "蓝色星球"}))
        payload = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                                "duration": "3.5", "avg_frame_rate": "25/1"}]}
        catalog = Catalog(self.database, self.library, probe_func=lambda path: payload)
        catalog.refresh()
        with closing(sqlite3.connect(self.database)) as conn:
            before_meta = conn.execute("SELECT * FROM catalog_meta").fetchall()
            before_assets = conn.execute("SELECT * FROM assets").fetchall()
        before_files = {path.relative_to(self.library): (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in self.library.rglob("*") if path.is_file()}

        catalog.import_legacy_metadata(workbook)

        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT * FROM catalog_meta").fetchall(), before_meta)
            self.assertEqual(conn.execute("SELECT * FROM assets").fetchall(), before_assets)
        self.assertEqual({path.relative_to(self.library): (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in self.library.rglob("*") if path.is_file()}, before_files)
        before_read = self.database.read_bytes()
        self.assertEqual(catalog.eligible_assets()[0].descriptions[0].text, "蓝色星球")
        self.assertEqual(self.database.read_bytes(), before_read)

    def test_new_assets_can_be_enriched_by_reimport_after_a_later_refresh(self):
        self.add_video("first/BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "蓝色星球"}))
        self.catalog.import_legacy_metadata(self.workbook)
        self.add_video("later/BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        before = self.catalog.list_assets()
        self.assertEqual([len(asset.descriptions) for asset in before], [1, 0])

        self.catalog.import_legacy_metadata(self.workbook)

        after = self.catalog.list_assets()
        self.assertEqual([asset.asset_id for asset in after], [asset.asset_id for asset in before])
        self.assertEqual([len(asset.descriptions) for asset in after], [1, 1])

    def test_filename_identifier_is_not_a_substring_or_directory_match(self):
        for relative_path in ("BJ1_earth.mp4", "BJ10_galaxy.mp4", "BJ1A_invalid.mp4",
                              "prefixBJ1_invalid.mp4", "BJ1/ordinary.mp4"):
            self.add_video(relative_path)
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "唯一匹配"}))

        self.assertEqual(self.catalog.import_legacy_metadata(self.workbook), 1)

        self.assertEqual([asset.relative_path.as_posix() for asset in self.catalog.list_assets()
                          if asset.descriptions], ["BJ1_earth.mp4"])

    def test_complete_legacy_identifiers_bind_only_at_filename_start(self):
        expected = {
            "BJ901_旧编号.mp4": "旧编号",
            "first/BJ901-1_地球.mp4": "地球",
            "second/bj901-1_地球.mov": "地球",
            "BJ901-2_太空.mp4": "太空",
            "BJ901-10_远景.mp4": "远景",
            "BJ906-4546_星系.mp4": "星系",
            "BJ826-1000_地球太空.mp4": "地球太空1000",
            "BJ826-1015_地球太空.mp4": "地球太空1015",
            "BJ901-1A_无效.mp4": None,
            "BJ901-1-2_无效.mp4": None,
            "BJ901-1_目录/普通素材.mp4": None,
            "prefixBJ901-1_无效.mp4": None,
            "BJ901A_无效.mp4": None,
            "BJ901-_无效.mp4": None,
            "BJ９０２-1_无效.mp4": None,
            "BJ902-1_无有效行.mp4": None,
        }
        for relative_path in expected:
            self.add_video(relative_path)
        self.catalog.refresh(probe=False)
        identifiers = (
            (" BJ901 ", "旧编号"), ("\n bj901-1 \t", "地球"), ("BJ901-2", "太空"),
            ("BJ901-10", "远景"), ("BJ906-4546", "星系"),
            ("BJ826-1000", "地球太空1000"), ("BJ826-1015", "地球太空1015"),
            ("随笔 BJ902-1", "无效子串"), ("BJ902-1A", "无效后缀"),
            ("BJ902-1-2", "无效多段"), ("BJ９０２-1", "无效非ASCII"),
        )
        self.workbook.write_bytes(_make_xlsx({
            ref: text for row, (identifier, description) in enumerate(identifiers, start=2)
            for ref, text in ((f"M{row}", identifier), (f"E{row}", description))
        }))

        count = self.catalog.import_legacy_metadata(self.workbook)

        assets = self.catalog.list_assets()
        for asset in assets:
            relative_path = asset.relative_path.as_posix()
            with self.subTest(relative_path=relative_path):
                description = expected[relative_path]
                self.assertEqual([item.text for item in asset.descriptions],
                                 [description] if description is not None else [])
        self.assertEqual(count, 8)
        same_identifier = [asset for asset in assets if asset.relative_path.parts[0] in ("first", "second")]
        self.assertEqual(len({asset.asset_id for asset in same_identifier}), 2)

    def test_import_requires_an_existing_catalog_without_creating_a_database(self):
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "地球"}))

        with self.assertRaisesRegex(FileNotFoundError, "目录数据库不存在"):
            self.catalog.import_legacy_metadata(self.workbook)

        self.assertFalse(self.database.parent.exists())

    def test_import_rejects_root_mismatch_and_unknown_schema_without_mutation(self):
        self.add_video("BJ1_地球.mp4")
        self.catalog.refresh(probe=False)
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "地球"}))
        another_root = self.root / "other_library"
        another_root.mkdir()
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "素材库不一致"):
            Catalog(self.database, another_root).import_legacy_metadata(self.workbook)
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("UPDATE catalog_meta SET schema_version = 999")
        before = self.database.read_bytes()

        with self.assertRaisesRegex(ValueError, "schema.*999"):
            self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual(self.database.read_bytes(), before)

    def test_import_rejects_database_redirected_inside_library(self):
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "地球"}))
        original_resolve = Path.resolve

        def redirected(path, *args, **kwargs):
            if path == self.database:
                return self.library / "unsafe.sqlite3"
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", redirected):
            with self.assertRaisesRegex(ValueError, "目录数据库不能位于素材库内"):
                self.catalog.import_legacy_metadata(self.workbook)

        self.assertEqual(list(self.library.iterdir()), [])
        self.assertFalse(self.database.exists())

    def test_old_schemas_read_and_import_without_health_or_schema_changes(self):
        source = self.add_video("BJ1_地球.mp4")
        self.workbook.write_bytes(_make_xlsx({"M2": "BJ1", "E2": "蓝色星球"}))
        for version in (1, 2, 3):
            with self.subTest(version=version):
                database = self.root / f"version_{version}.sqlite3"
                catalog = Catalog(database, self.library)
                asset_id = hashlib.sha256(
                    f"{catalog.root_id}\0{ntpath.normcase(source.name)}".encode("utf-8")
                ).hexdigest()
                generation = ", scan_generation INTEGER NOT NULL DEFAULT 0" if version >= 2 else ""
                error = ", probe_error TEXT" if version >= 3 else ""
                with closing(sqlite3.connect(database)) as conn, conn:
                    conn.execute("CREATE TABLE catalog_meta (schema_version INTEGER NOT NULL, "
                                 f"root_path TEXT NOT NULL{generation})")
                    conn.execute("INSERT INTO catalog_meta (schema_version, root_path) VALUES (?, ?)",
                                 (version, catalog.root_path))
                    conn.execute("CREATE TABLE assets (asset_id TEXT PRIMARY KEY, "
                                 "relative_path TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL, "
                                 "mtime_ns INTEGER NOT NULL, probe_status TEXT NOT NULL, "
                                 "duration_seconds REAL, width INTEGER, height INTEGER, "
                                 "frame_rate REAL, video_codec TEXT, audio_codec TEXT"
                                 f"{generation}{error})")
                    conn.execute("INSERT INTO assets (asset_id, relative_path, size_bytes, mtime_ns, "
                                 "probe_status) VALUES (?, 'BJ1_地球.mp4', ?, ?, 'skipped')",
                                 (asset_id, source.stat().st_size, source.stat().st_mtime_ns))
                    before_meta = conn.execute("SELECT * FROM catalog_meta").fetchall()
                    before_assets = conn.execute("SELECT * FROM assets").fetchall()
                before = database.read_bytes()
                self.assertEqual(catalog.list_assets()[0].descriptions, ())
                self.assertEqual(database.read_bytes(), before)

                catalog.import_legacy_metadata(self.workbook)

                self.assertEqual(catalog.list_assets()[0].descriptions[0].text, "蓝色星球")
                with closing(sqlite3.connect(database)) as conn:
                    self.assertEqual(conn.execute("SELECT * FROM catalog_meta").fetchall(), before_meta)
                    self.assertEqual(conn.execute("SELECT * FROM assets").fetchall(), before_assets)
                catalog.refresh(probe=False)
                self.assertEqual(catalog.list_assets()[0].descriptions[0].text, "蓝色星球")
                with closing(sqlite3.connect(database)) as conn:
                    self.assertEqual(conn.execute("SELECT schema_version FROM catalog_meta").fetchone(),
                                     (3,))


if __name__ == "__main__":
    unittest.main()
