# -*- mode: python ; coding: utf-8 -*-
# ============================================================
#  水星配音对齐工作室 · 轻量云配版 · PyInstaller spec（含完整防护层）
#
#  这份 spec 明确带上：
#    - licensing 完整子包（激活码 gate + HWID + Nonce/HMAC + DPAPI）
#    - RASP 完整子模块（调试器/frida/vm/完整性 检测）
#    - web/ 前端资源（含激活遮罩层 index.html）
#    - app.ico 图标（Windows exe 图标 + 任务栏 + 快捷方式）
#
#  运行时靠 启动软件.bat 里的两个环境变量最终启用防护：
#    set DUB_ALIGN_LICENSE_REQUIRED=1   -> 激活码 gate 生效
#    set DUB_ALIGN_RASP_STRICT=1         -> 反调试/反注入 exit(3)
#  （不设 = 开发/兼容模式，gate 放行）
# ============================================================
from pathlib import Path

# spec 位于 installer\，项目根是它的上一级
HERE = Path(SPEC).resolve().parent.parent
SOURCE = HERE / "source"
ICON = HERE / "installer" / "app.ico"

# 显式列出 licensing 子模块 —— 保底防止 PyInstaller 静态分析漏
LICENSING_MODS = [
    "dub_align_studio.licensing",
    "dub_align_studio.licensing.client",
    "dub_align_studio.licensing.crypto_store",
    "dub_align_studio.licensing.heartbeat",
    "dub_align_studio.licensing.machine_id",
    "dub_align_studio.licensing.rasp",
    "dub_align_studio.licensing.session",
]

# 主程序常用但可能被静态分析漏的模块
EXTRA_HIDDEN = [
    "dub_align_studio",
    "dub_align_studio.web_server",
    "dub_align_studio.syspy",
    "dub_align_studio.native_window",
]

# 【关键】_build_info 只在函数体里 try/except 懒加载，PyInstaller 静态分析看不到，
# 必须显式列进 hiddenimports，否则不会被打进 PYZ → 运行时 import 失败 →
# PACKAGED=False → 激活码 gate / RASP / 完整性校验 全部退化到"开发模式"（默认关）。
# 这是之前"没输激活码也能进""源码运行"标签的根因。
# prepare_build_info() 在 pyinstaller 之前已把 source/dub_align_studio/_build_info.py 写好，
# 所以打包时该模块一定存在，可以安全加入 hiddenimports。
if (SOURCE / "dub_align_studio" / "_build_info.py").exists():
    EXTRA_HIDDEN.append("dub_align_studio._build_info")
else:
    print("[spec][!] source/dub_align_studio/_build_info.py 不存在 —— "
          "打包版将无防护！请先跑 build_dist.prepare_build_info()")

# 数据文件（web 前端资源必须一起打）
DATAS = [(str(SOURCE / "dub_align_studio" / "web"), "dub_align_studio/web")]

# fonts 目录如果存在也带上
_fonts = SOURCE / "dub_align_studio" / "fonts"
if _fonts.exists():
    DATAS.append((str(_fonts), "dub_align_studio/fonts"))

# app.ico 塞进包内，让 native_window 运行时能找到
_icon_for_pack = HERE / "installer" / "app.ico"
if _icon_for_pack.exists():
    DATAS.append((str(_icon_for_pack), "."))

# ---------- pywebview 全套收集（原生窗口模式必需） ----------
# 用 collect_all 一次性带上 webview 子包 + 数据 + 平台绑定 dll
# 若打包机没装 pywebview，就打成"只有浏览器模式"的 exe（运行时自动降级）
_extra_binaries = []
_extra_datas = []
_extra_hidden = []
try:
    from PyInstaller.utils.hooks import collect_all as _collect_all
    _d, _b, _h = _collect_all("webview")
    _extra_datas.extend(_d)
    _extra_binaries.extend(_b)
    _extra_hidden.extend(_h)
    # 兜底：把 Windows 常用后端显式列上
    _extra_hidden.extend([
        "webview",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",
        "webview.platforms.winforms",
        "clr_loader",
    ])
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] pywebview 未装/收集失败 ({_exc}) → 包内不带原生窗口能力，只能浏览器")

# ---------- certifi CA 证书（HTTPS 必需！） ----------
# 冻结 exe 里若不带 CA 证书，所有 HTTPS 请求（Edge TTS worker.dev、ggml 模型下载
# 从 HF/GitHub）都会失败——而 license 服务器是 HTTP 所以不受影响，正好解释
# 「license 通、Edge TTS/下载全挂」。把 certifi 的 cacert.pem 打进去。
try:
    from PyInstaller.utils.hooks import collect_all as _collect_all2
    _cd, _cb, _ch = _collect_all2("certifi")
    _extra_datas.extend(_cd)
    _extra_binaries.extend(_cb)
    _extra_hidden.extend(_ch)
    _extra_hidden.extend(["certifi", "ssl", "_ssl"])
    print("[spec] certifi CA 证书已收集（HTTPS 可用）")
except Exception as _exc:  # noqa: BLE001
    print(f"[spec] certifi 收集失败 ({_exc}) → HTTPS 可能不可用；pip install certifi 后重打")

DATAS = DATAS + _extra_datas

a = Analysis(
    [str(SOURCE / "dub_align_studio" / "launcher.py")],
    pathex=[str(SOURCE)],
    binaries=_extra_binaries,
    datas=DATAS,
    hiddenimports=LICENSING_MODS + EXTRA_HIDDEN + _extra_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    # -OO：剥离所有 docstring + assert，反编译出来的代码更难读
    # （licensing 无 assert 安全逻辑，剥离不影响功能——已核查）
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='水星配音对齐工作室_云配版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX 压缩会触发一部分杀软误报，留原始
    console=False,   # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='水星配音对齐工作室_云配版',
)
