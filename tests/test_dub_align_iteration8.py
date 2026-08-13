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

    #: 覆盖所有可能的 Python 命令前缀（含 %PYEXE% / !PYEXE! 变量展开、py -3、
    #: python3.exe 等），确保长命令不会因正则前缀不匹配而**逃过检查**。
    _PY_C_RE = re.compile(
        r'(?:'
        r'python(?:3)?(?:\.exe)?'    # python / python3 / python.exe / python3.exe
        r'|py(?:\s+-3)?'             # py / py -3
        r'|%[A-Za-z_]\w*%'           # %PYEXE% 之类环境变量展开
        r'|![A-Za-z_]\w*!'           # !PYEXE! 延迟展开
        r')'
        r'\s+-c\s+"([^"]*)"',        # 匹配全长，不设上限——长命令必被抓到
        re.IGNORECASE,
    )

    def _bat(self) -> str:
        """读取主打包脚本 `项目打包.bat`；缺失时 skip（不假成功）。"""
        root = Path(__file__).resolve().parents[1]
        for name in ("项目打包.bat", "打包.bat"):  # 新名优先，兼容旧名
            for cand in (Path(name), root / name):
                if cand.exists():
                    return cand.read_text(encoding="utf-8")
        self.skipTest("项目打包.bat / 打包.bat 均不在此仓库根")

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

    def test_py_c_regex_matches_all_prefix_forms(self):
        """自检：`_PY_C_RE` 必须能匹配到 python / python.exe / py / py -3 /
        python3 / %PYEXE% / !PYEXE! 各种前缀的 `-c "..."`，防止未来有人加了
        `%PYEXE% -c "shutil.make_archive(...)"` 因前缀不匹配而逃过检查。"""
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
        ]
        for bat_line, expected in samples:
            hits = self._PY_C_RE.findall(bat_line)
            self.assertEqual(
                len(hits), 1,
                f"前缀应被匹配，实际未匹配：{bat_line!r}",
            )
            self.assertEqual(hits[0], expected, bat_line)

    def test_finalize_script_present_and_valid(self):
        for cand in (Path("打包收尾.py"), Path(__file__).resolve().parents[1] / "打包收尾.py"):
            if cand.exists():
                src = cand.read_text(encoding="utf-8")
                self.assertIn("make_archive", src)   # 生成压缩包
                self.assertIn("发布包", src)          # 输出目录
                self.assertIn("APP_VERSION", src)    # 文件名含版本号
                self.assertIn("版本.txt", src)        # 写版本文件
                compile(src, str(cand), "exec")      # 语法可编译
                return
        self.skipTest("打包收尾.py 不在此仓库根")


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
