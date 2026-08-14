"""第八轮迭代 UI 契约：色号删除 / 文本框样式模板 / 素材库管理 / 文本框页重排 / 打包自检。"""

import re
import unittest
from pathlib import Path

from dub_align_studio import web_server


class UiContractV8Tests(unittest.TestCase):
    def setUp(self):
        self.html = (Path(web_server.__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

    def test_color_delete_wired(self):
        self.assertIn("delColor", self.html)         # 自定义色号删除
        self.assertIn("★ 存色", self.html)

    def test_overlay_template_wired(self):
        for m in ('id="tplSel"', "saveTpl", "applyTpl", "renameTpl", "delTpl", "curStyle", "样式模板"):
            self.assertIn(m, self.html, m)

    def test_asset_library_manager_wired(self):
        for m in ('id="assetList"', "renderAssetList", "delAsset", "toggleAssets", "素材库"):
            self.assertIn(m, self.html, m)

    def test_p2_layout_three_columns(self):
        # 编辑文本框为独立主列；右列堆叠（2026-07-26：字幕样式移到一键成片，右列改为待编辑队列 + 已添加）
        self.assertIn("主列：编辑文本框", self.html)
        self.assertIn("右列：待编辑队列 + 已添加", self.html)
        # 已添加表格去掉时间窗列后为 5 列（4 表头 + 操作列）
        self.assertIn('colspan="5"', self.html)


class PackagingScriptTests(unittest.TestCase):
    """打包脚本契约。**契约范围仅限主打包脚本 `项目打包.bat`**——它是唯一约定
    "收尾逻辑走独立 `打包收尾.py`、bat 只做流程编排"的路径。其它 bat（轻量云配版
    / 整合离线版 / 一键 / Inno 安装程序打包）走各自独立打包路径，需要直接 `python
    -c` 调软件模块拿 base_prefix / data_root / APP_VERSION，这属于各自 bat 的合法
    实现，本套契约不覆盖。若将来要扩到全部 bat，请把每个 bat 的复杂 python 逻辑
    也迁到独立 .py 收尾脚本，并给每个 bat 加分别的 test。"""

    #: 允许在 `项目打包.bat` 里出现的 `<py-cmd> -c "…"` 的**精确**内容白名单。
    #: 值以 strip() 归一化后严格等值比较；新增合法探测必须加进这里（防误伤但也防漏）。
    ALLOWED_INLINE_PY_C: set[str] = {
        "import sys",
    }

    #: **当前明确支持**的 Python 命令前缀（共 9 种，含带引号绝对路径）——
    #: 若未来打包脚本用了其它前缀，先在 test_py_c_regex_matches_supported_prefixes
    #: 里加对应样本、再改这里；宁可扩正则也不要留缺口让长命令逃过白名单检查。
    #: 支持的前缀：
    #:   1) python           2) python.exe
    #:   3) python3          4) python3.exe
    #:   5) py               6) py -3
    #:   7) %VAR%            （环境变量展开，如 %PYEXE%）
    #:   8) !VAR!            （延迟变量展开，如 !PYEXE!）
    #:   9) "C:\...\python.exe"  （带引号绝对路径，如 setup 打包机的 Python 装在
    #:                            "C:\Program Files\Python311\python.exe"）
    _PY_C_RE = re.compile(
        r'(?:'
        r'"[^"]*python(?:3)?(?:\.exe)?"'   # 9) 带引号的绝对/相对路径
        r'|python(?:3)?(?:\.exe)?'          # 1)-4) 裸命令名
        r'|py(?:\s+-3)?'                    # 5)-6) py / py -3
        r'|%[A-Za-z_]\w*%'                  # 7) 环境变量展开
        r'|![A-Za-z_]\w*!'                  # 8) 延迟变量展开
        r')'
        r'\s+-c\s+"([^"]*)"',               # 匹配全长，长命令必被抓到
        re.IGNORECASE,
    )

    def _bat(self) -> str:
        """严格读取仓库根 `项目打包.bat`——契约文件；缺失即测试**失败**（不 skip）。

        `打包.bat` 旧名兼容已随契约收紧而移除：本套契约只约束仓库根这一个文件。"""
        cand = Path(__file__).resolve().parents[1] / "项目打包.bat"
        self.assertTrue(
            cand.is_file(),
            f"`项目打包.bat` 是本契约的必要文件，但不在仓库根：{cand}",
        )
        return cand.read_text(encoding="utf-8")

    def _finalize_script_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "打包收尾.py"

    def test_finalize_script_exists(self):
        """契约必要文件：`打包收尾.py` 是 `项目打包.bat` 迁出的收尾脚本；
        缺失说明契约已破，必须**失败**（不 skip）。"""
        path = self._finalize_script_path()
        self.assertTrue(
            path.is_file(),
            f"`打包收尾.py` 是本契约的必要文件，但不在仓库根：{path}",
        )

    def test_project_pack_bat_cleans_stale_and_selfchecks(self):
        bat = self._bat()
        self.assertIn("__pycache__", bat)      # 清旧字节码
        self.assertIn(":SELFFAIL", bat)         # 自检失败分支（收尾脚本非0退出触发）
        self.assertIn("打包收尾.py", bat)        # 自检/版本/压缩包收尾在独立脚本

    def test_project_pack_bat_uses_finalize_script(self):
        """契约（仅 `项目打包.bat`）：收尾逻辑（压缩包/版本文件/自检）必须在
        独立 `打包收尾.py` 里，不能内联到 bat——cmd 对复杂引号会拆断。

        允许在 bat 里出现**已白名单的短探测**（如 `python -c "import sys"` 用于
        判定解释器是否可用）；长命令由白名单精确等值判定挡下，无法因正则不匹配
        而逃逸。任务书明确要求"不要为了让本任务变绿而删除正常的 Python 探测逻辑"。
        """
        bat = self._bat()
        self.assertIn("打包收尾.py", bat)
        # 收尾操作绝不能内联到 bat（防旧写法回潮）
        self.assertNotIn("make_archive", bat)
        self.assertNotIn("shutil.make_archive", bat)
        # 遍历所有 `<py-cmd> -c "..."`（全长匹配，长命令必被抓到）；严格白名单等值判定
        offenders: list[str] = []
        for match in self._PY_C_RE.finditer(bat):
            inline = match.group(1).strip()
            if inline not in self.ALLOWED_INLINE_PY_C:
                offenders.append(inline)
        self.assertEqual(
            offenders, [],
            f"`项目打包.bat` 里的 `<py> -c \"...\"` 只允许白名单短探测 "
            f"{sorted(self.ALLOWED_INLINE_PY_C)!r}；发现未白名单：\n"
            + "\n".join(f"  · {x!r}" for x in offenders)
            + "\n（新增合法探测请加进 PackagingScriptTests.ALLOWED_INLINE_PY_C。）"
        )

    def test_py_c_regex_matches_supported_prefixes(self):
        """自检：`_PY_C_RE` 必须能匹配到**当前明确支持的 9 种前缀**——
        python / python.exe / python3 / python3.exe / py / py -3 / %PYEXE% /
        !PYEXE! / "C:\\...\\python.exe"（带引号绝对路径）。

        这不是"所有可能前缀"，而是当前枚举支持的清单。若打包脚本将来用了
        清单外的形式，先加进 samples 让此测试提示，再回去扩正则。"""
        samples = [
            ('python -c "import sys"', "import sys"),
            ('python.exe -c "print(1)"', "print(1)"),
            ('python3 -c "x"', "x"),
            ('python3.exe -c "y"', "y"),
            ('py -c "z"', "z"),
            ('py -3 -c "aa"', "aa"),
            ('%PYEXE% -c "shutil.make_archive(\'a\',\'zip\',\'b\',\'c\')"',
             "shutil.make_archive('a','zip','b','c')"),
            ('!PYEXE! -c "bb"', "bb"),
            # 带引号绝对路径（Windows 打包机常见），必须也被抓到，避免长命令逃逸
            # 用非 raw 字符串以便 \\ 表示反斜杠、\' 表示单个单引号（raw 里 \' 是两字符）
            ('"C:\\Program Files\\Python311\\python.exe" -c "shutil.make_archive(\'z\')"',
             "shutil.make_archive('z')"),
            (r'"D:\Python\python3.exe" -c "long stuff here"',
             "long stuff here"),
        ]
        for bat_line, expected in samples:
            hits = self._PY_C_RE.findall(bat_line)
            self.assertEqual(
                len(hits), 1,
                f"前缀应被匹配，实际未匹配：{bat_line!r}",
            )
            self.assertEqual(hits[0], expected, bat_line)

    def test_finalize_script_valid(self):
        """`打包收尾.py` 是本契约必要文件（其存在性由 test_finalize_script_exists 保证）；
        本测试进一步检查内容契约：压缩包/输出目录/版本文件三个关键字必须都在，
        且脚本本身语法可编译。"""
        cand = self._finalize_script_path()
        self.assertTrue(cand.is_file(),
                         f"`打包收尾.py` 必须存在（本契约必要文件），未找到：{cand}")
        src = cand.read_text(encoding="utf-8")
        self.assertIn("make_archive", src)   # 生成压缩包
        self.assertIn("发布包", src)          # 输出目录
        self.assertIn("APP_VERSION", src)    # 文件名含版本号
        self.assertIn("版本.txt", src)        # 写版本文件
        compile(src, str(cand), "exec")      # 语法可编译


class VersionedZipLogicTests(unittest.TestCase):
    def test_archive_names_with_version_and_contains_product(self):
        import datetime
        import shutil
        import tempfile
        import zipfile
        from pathlib import Path

        from dub_align_studio import version

        work = Path(tempfile.mkdtemp(prefix="zip_"))
        try:
            prod = work / "dist" / "水星配音对齐工作室"
            prod.mkdir(parents=True)
            (prod / "水星配音对齐工作室.exe").write_text("x", encoding="utf-8")
            name = ("水星配音对齐工作室_v" + version.APP_VERSION + "_" +
                    datetime.datetime(2026, 7, 23, 15, 30).strftime("%Y%m%d_%H%M"))
            (work / "发布包").mkdir()
            archive = shutil.make_archive(str(work / "发布包" / name), "zip",
                                          str(work / "dist"), "水星配音对齐工作室")
            self.assertTrue(archive.endswith(".zip"))
            self.assertIn(version.APP_VERSION, Path(archive).name)  # 文件名含版本号 → 可区分
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
            self.assertIn("水星配音对齐工作室/水星配音对齐工作室.exe", names)
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
