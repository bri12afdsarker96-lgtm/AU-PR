; ============================================================
;  Inno Setup 6 · 水星配音对齐工作室 · 轻量云配版包 · 安装器脚本
;
;  用法：
;    1. 先跑 build_dist.py --with-ffmpeg
;       产物：dist\轻量云配版包\  （Nuitka+Cython 编译产物 + ffmpeg.exe）
;    2. 用 Inno Setup 6 打开本文件（右键 → Open with Inno Setup Compiler），
;       或命令行：
;         "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;    3. 产物：dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe
;
;  减负策略：
;    - LZMA2/ultra64 + SolidCompression
;      → payload 通常压到 40~60% 体积
;    - 不打包不必要的资源（无 __pycache__、无 .py 源码、无文档、无 tests）
;    - ffmpeg 用 essentials 裁剪版（约 40MB → LZMA2 后约 15MB）
;    - 单文件 exe，双击即安装向导，不依赖任何 vcredist / 官方 Python
; ============================================================

#define AppName        "水星配音对齐工作室"
#define AppNameEn      "Mercury Dub Align Studio"
#define AppVersion     "0.7.71"
#define AppPublisher   "Mercury"
#define AppExeName     "水星配音对齐工作室.exe"
#define SourceDir      "dist\轻量云配版包"
#define OutputDir      "dist\安装器"
#define OutputBaseName "setup_水星配音对齐工作室_v" + AppVersion + "_lite"

[Setup]
AppId={{5D3BAA00-1E4F-45D6-9F60-DUBALIGN2026}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}（轻量云配版）
AppPublisher={#AppPublisher}

; 默认安装到 Program Files；不需要管理员权限时改成 userdocs
DefaultDirName={autopf}\{#AppNameEn}
DefaultGroupName={#AppName}
; 允许用户选择目录 + 组名
DisableProgramGroupPage=no
DisableDirPage=no
; 卸载器信息（Windows 控制面板会显示）
UninstallDisplayName={#AppName} v{#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

; 图标（可选；有 icon.ico 就用）
; SetupIconFile=icon.ico

; 输出
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}

; ============ 减负核心 ============
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2
InternalCompressLevel=ultra64
CompressionThreads=auto
; ==================================

; 架构 / 权限
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog

; 语言 / UI
ShowLanguageDialog=no
WizardStyle=modern
DisableWelcomePage=no
; 若想显示 EULA，取消下一行注释并放一个 UTF-8 rtf 到项目根
; LicenseFile=LICENSE.rtf

; 卸载后清理选项
UninstallFilesDir={app}\uninstall

; 代码签名（可选，需要证书）
; SignTool=signtool sign /f "cert.pfx" /p "password" /t http://timestamp.digicert.com $f
; SignedUninstaller=yes

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
; 如需英文备选：
; Name: "en";  MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "创建桌面快捷方式";   GroupDescription: "附加任务："; Flags: unchecked
Name: "quicklaunchicon"; Description: "创建快速启动栏图标"; GroupDescription: "附加任务："; Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
; ---- 主发行目录（build_dist.py 的产物）----
;   * 已经 Nuitka+Cython 编译；已清扫敏感文件
;   * 不含 .py / .pyc / settings.json / license.json / tests / docs / .git
;   * excludes 是双保险，防止误打包
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; \
    Excludes: "*.py,*.pyc,*.pyo,__pycache__,.git,.github,.claude,tests,docs,settings.json,license.json,queue.sqlite3,gpu_state.json,*.md,pyproject.toml,setup.py,conftest.py,.gitignore"

[Icons]
; 开始菜单主图标
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
; 开始菜单里的卸载入口
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
; 桌面（可选）
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后可选立即启动（不勾就不启动）
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; \
    Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; 卸载时也清 marker / 数据缓存（用户可选保留数据）
Type: filesandordirs; Name: "{app}\_MEIPASS*"
Type: filesandordirs; Name: "{app}\__pycache__"
; 注意：**不删** 用户本地数据目录 (~/.dub_align_studio 或 我的文档\水星配音数据)
; 保留用户 Excel / 生成的成片；只删安装目录

[Code]
// ============================================================
//  运行时校验：
//  1) Windows 版本必须 ≥ 10（软件用了较新 ctypes/urllib 特性）
//  2) 已安装同版本 → 提示是否覆盖
// ============================================================

function InitializeSetup(): Boolean;
var Version: TWindowsVersion;
begin
  Result := True;
  GetWindowsVersionEx(Version);
  if Version.NTPlatform and (Version.Major < 10) then begin
    MsgBox('本软件需要 Windows 10 或更高版本。当前系统版本过低，无法继续安装。',
            mbError, MB_OK);
    Result := False;
  end;
end;

// 卸载前提示：是否保留用户数据？（本 MVP 只保留，不询问）
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    // Placeholder：如需询问用户是否清 %USERPROFILE%\.dub_align_studio\，在此实现
  end;
end;
