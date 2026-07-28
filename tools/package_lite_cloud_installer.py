from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PRODUCT_NAME = "水星配音对齐工作室"
REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "source"
LITE_ROOT = REPO / "轻量云配版包"
ISS_FILE = REPO / "installer" / f"{PRODUCT_NAME}.iss"


def log(message: str) -> None:
    print(message, flush=True)


def app_version() -> str:
    sys.path.insert(0, str(SOURCE))
    try:
        from dub_align_studio.version import APP_VERSION

        return str(APP_VERSION)
    finally:
        try:
            sys.path.remove(str(SOURCE))
        except ValueError:
            pass


def find_iscc() -> Path | str | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Inno Setup 6" / "ISCC.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    from shutil import which

    found = which("iscc")
    return found or None


def ensure_icon() -> None:
    icon = REPO / "installer" / "app.ico"
    if icon.is_file():
        return
    maker = REPO / "installer" / "make_icon.py"
    if maker.is_file():
        log("[icon] creating installer icon")
        subprocess.run([sys.executable, str(maker)], cwd=str(REPO), check=True)


def main() -> int:
    version = app_version()
    src = LITE_ROOT / f"{PRODUCT_NAME}_轻量版_v{version}"
    launcher = src / "启动.bat"
    log(f"[version] v{version}")
    log(f"[source] {src}")
    if not launcher.is_file():
        log("[ERROR] Lite cloud package was not found.")
        log("        Run 打包_轻量云配版.bat first, then run this installer script again.")
        return 1
    if not ISS_FILE.is_file():
        log(f"[ERROR] Missing Inno Setup script: {ISS_FILE}")
        return 1
    ensure_icon()
    iscc = find_iscc()
    if iscc is None:
        log("[ERROR] Inno Setup 6 was not found.")
        log("        Install Inno Setup 6, then run this script again.")
        return 1
    log(f"[iscc] {iscc}")
    cmd = [
        str(iscc),
        "/DLite=1",
        f"/DMyVer={version}",
        f"/DRepoDir={REPO}",
        f"/DSrcDir={src}",
        str(ISS_FILE),
    ]
    log("[build] creating installer")
    completed = subprocess.run(cmd, cwd=str(REPO))
    if completed.returncode != 0:
        log(f"[ERROR] Inno Setup failed with exit code {completed.returncode}.")
        return completed.returncode
    out = REPO / "发布包" / f"{PRODUCT_NAME}_云配版安装程序_v{version}.exe"
    if out.is_file():
        mb = out.stat().st_size / 1024 / 1024
        log(f"[done] installer: {out}")
        log(f"[size] {mb:.1f} MB")
    else:
        log("[done] Inno Setup finished, but the expected output file was not found.")
        log("       Check the 发布包 folder for the generated installer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
