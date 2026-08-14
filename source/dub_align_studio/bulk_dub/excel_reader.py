"""Excel 批量任务导入：从 A 列（视频路径）+ B 列（口播文案）读行。

R8 加固：
    - ZIP bomb 防护：条目数上限、单条解压大小上限、总解压大小上限；
    - 预览上限（`preview.rows` 只保留前 N 条；总统计仍是全表）。

沿用项目现有 xlsx_reader 的解析基础（纯标准库，zipfile + xml.etree），不引入新依赖。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")

VIDEO_EXTS = frozenset({
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
    ".flv", ".wmv", ".mpg", ".mpeg", ".ts",
})

# R8 ZIP bomb 硬上限
MAX_ZIP_ENTRIES = 100           # 一个 xlsx 通常 < 20 个 xml；100 已很宽松
MAX_ZIP_TOTAL_UNPACKED = 200 * 1024 * 1024   # 200 MB
MAX_ZIP_ONE_UNPACKED = 64 * 1024 * 1024      # 64 MB
MAX_XLSX_UPLOAD_BYTES = 20 * 1024 * 1024     # 20 MB 上传上限；api 层也强制

# 预览返回上限
DEFAULT_PREVIEW_LIMIT = 500

# 单批任务数上限（防误导入亿级 xlsx）
MAX_TASKS_PER_BATCH = 100000


class ExcelSizeError(ValueError):
    """xlsx 结构越界（ZIP bomb 防护）。"""


@dataclass(frozen=True)
class ExcelRow:
    row_number: int
    video_path: str
    text: str
    valid: bool
    reason: str = ""
    text_preview: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "video_path": self.video_path,
            "text": self.text,
            "text_preview": self.text_preview,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass
class PreviewResult:
    rows: list[ExcelRow] = field(default_factory=list)
    total: int = 0
    valid: int = 0
    invalid: int = 0
    skipped_empty: int = 0
    sheet_name: str = ""

    def to_dict(self, preview_limit: int = DEFAULT_PREVIEW_LIMIT) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows[:preview_limit]],
            "rows_total": len(self.rows),
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "skipped_empty": self.skipped_empty,
            "sheet_name": self.sheet_name,
            "preview_limit": preview_limit,
        }


def _open_bundle(data: bytes) -> zipfile.ZipFile:
    """带 ZIP bomb 防护的打开：检查条目数 + 每条 file_size + 总大小。"""
    try:
        bundle = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("不是有效的 xlsx 文件（无法按 zip 打开）。") from exc
    infos = bundle.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        bundle.close()
        raise ExcelSizeError(
            f"xlsx 条目过多（{len(infos)} > {MAX_ZIP_ENTRIES}）——可能是 ZIP bomb"
        )
    total = 0
    for info in infos:
        if info.file_size > MAX_ZIP_ONE_UNPACKED:
            bundle.close()
            raise ExcelSizeError(
                f"xlsx 单条目解压过大：{info.filename} → {info.file_size} 字节"
            )
        total += info.file_size
        if total > MAX_ZIP_TOTAL_UNPACKED:
            bundle.close()
            raise ExcelSizeError(
                f"xlsx 总解压大小过大：{total} > {MAX_ZIP_TOTAL_UNPACKED}"
            )
    return bundle


def parse_excel(source: bytes | str | Path,
                *, check_exists: bool = True) -> PreviewResult:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if len(data) > MAX_XLSX_UPLOAD_BYTES:
        raise ExcelSizeError(
            f"xlsx 文件太大（{len(data)} 字节 > {MAX_XLSX_UPLOAD_BYTES}）"
        )
    bundle = _open_bundle(data)
    with bundle:
        shared = _shared_strings(bundle)
        sheet_name = _first_sheet_path(bundle)
        try:
            root = ET.fromstring(bundle.read(sheet_name))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError(f"无法解析工作表：{sheet_name}") from exc

        rows_by_number: dict[int, dict[str, str]] = {}
        for cell in root.iterfind(".//m:sheetData/m:row/m:c", _NS):
            ref = cell.get("r") or ""
            m = _CELL_REF.match(ref)
            if not m:
                continue
            col = m.group(1)
            r_num = int(m.group(2))
            text = _cell_text(cell, shared)
            if text:
                rows_by_number.setdefault(r_num, {})[col] = text

    result = PreviewResult(sheet_name=sheet_name)
    if not rows_by_number:
        return result

    max_row = max(rows_by_number.keys())
    for r_num in range(2, max_row + 1):
        cells = rows_by_number.get(r_num) or {}
        a = (cells.get("A") or "").strip()
        b = (cells.get("B") or "").strip()
        if not a and not b:
            result.skipped_empty += 1
            continue
        row = _validate_row(r_num, a, b, check_exists=check_exists)
        result.rows.append(row)
        if row.valid:
            result.valid += 1
        else:
            result.invalid += 1
    result.total = result.valid + result.invalid
    return result


def _validate_row(row_number: int, video_path: str, text: str, *,
                  check_exists: bool) -> ExcelRow:
    preview = _text_preview(text)
    if not video_path:
        return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                        valid=False, reason="A 列为空（未填写视频路径）", text_preview=preview)
    if not text:
        return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                        valid=False, reason="B 列为空（未填写口播文案）", text_preview=preview)
    ext = Path(video_path).suffix.lower()
    if ext not in VIDEO_EXTS:
        return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                        valid=False, reason=f"视频格式不支持：{ext or '（无后缀）'}",
                        text_preview=preview)
    if check_exists:
        try:
            p = Path(video_path)
            if not p.exists():
                return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                                valid=False, reason="视频文件不存在", text_preview=preview)
            if not p.is_file():
                return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                                valid=False, reason="路径不是文件", text_preview=preview)
        except OSError as exc:
            return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                            valid=False, reason=f"路径读取失败：{exc}", text_preview=preview)
    return ExcelRow(row_number=row_number, video_path=video_path, text=text,
                    valid=True, text_preview=preview)


def _text_preview(text: str, head: int = 24, tail: int = 6) -> str:
    text = text.strip().replace("\r\n", " ").replace("\n", " ")
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}…{text[-tail:]}"


def _first_sheet_path(bundle: zipfile.ZipFile) -> str:
    names = [n for n in bundle.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)]
    if not names:
        raise ValueError("xlsx 内没有工作表。")
    return sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))[0]


def _shared_strings(bundle: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(bundle.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    strings: list[str] = []
    for item in root.iterfind("m:si", _NS):
        strings.append("".join(t.text or "" for t in item.iterfind(".//m:t", _NS)))
    return strings


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t") or ""
    if kind == "s":
        value = cell.find("m:v", _NS)
        try:
            return shared[int(value.text)].strip() if value is not None else ""
        except (ValueError, IndexError, TypeError):
            return ""
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iterfind(".//m:t", _NS)).strip()
    value = cell.find("m:v", _NS)
    return (value.text or "").strip() if value is not None else ""


def build_minimal_xlsx(rows: list[tuple[str, str]], *, include_header: bool = True) -> bytes:
    """在内存里合成一个最小 xlsx，供单测使用。零第三方依赖。"""
    all_rows: list[tuple[str, str]] = []
    if include_header:
        all_rows.append(("视频路径", "口播文案"))
    all_rows.extend(rows)

    string_pool: list[str] = []
    string_index: dict[str, int] = {}

    def _sid(s: str) -> int:
        if s in string_index:
            return string_index[s]
        string_index[s] = len(string_pool)
        string_pool.append(s)
        return string_index[s]

    sheet_rows_xml: list[str] = []
    for i, (a, b) in enumerate(all_rows, start=1):
        cells = []
        if a:
            cells.append(f'<c r="A{i}" t="s"><v>{_sid(a)}</v></c>')
        if b:
            cells.append(f'<c r="B{i}" t="s"><v>{_sid(b)}</v></c>')
        sheet_rows_xml.append(f'<row r="{i}">{"".join(cells)}</row>')

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>' + "".join(sheet_rows_xml) + '</sheetData></worksheet>'
    )
    ss_items = "".join(
        f'<si><t xml:space="preserve">{_xml_escape(s)}</t></si>' for s in string_pool
    )
    ss_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(string_pool)}" uniqueCount="{len(string_pool)}">' + ss_items +
        '</sst>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        '</Relationships>'
    )
    top_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )

    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", top_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/sharedStrings.xml", ss_xml)
    return buf.getvalue()


def build_zip_bomb_payload(entries: int = MAX_ZIP_ENTRIES + 10) -> bytes:
    """给测试用：构造一个条目数超限的 zip（不真的 bomb，只是超过 MAX_ZIP_ENTRIES）。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(entries):
            zf.writestr(f"entry_{i}.xml", "<x/>")
    return buf.getvalue()


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&apos;"))
