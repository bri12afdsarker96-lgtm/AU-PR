; 水星配音对齐工作室 · 整合离线版 安装程序（Inno Setup 6）
; 不要直接双击本文件——用仓库根目录的「打包安装程序.bat」编译，它会传入下面三个宏：
;   RepoDir = 仓库根绝对路径   SrcDir = 整合离线包内那个版本文件夹   MyVer = 版本号
#ifndef MyVer
  #define MyVer "0.0.0"
#endif
#ifndef RepoDir
  #define RepoDir "."
#endif
#ifndef SrcDir
  #define SrcDir "整合离线包\_请先用打包安装程序.bat_"
#endif
#define MyApp "水星配音对齐工作室"
#ifndef Lite
  #define Lite "0"
#endif
#if Lite == "1"
  #define Edition "（云配版）"
  #define OutName "水星配音对齐工作室_云配版安装程序_v" + MyVer
#else
  #define Edition ""
  #define OutName "水星配音对齐工作室_安装程序_v" + MyVer
#endif

[Setup]
; 显式 AppId：升级流程按此定位旧安装（每个版本共用同一 AppId → Inno 就地升级）；
; 云配版与整合离线版是不同产品，各自独立 AppId，互不覆盖。
#if Lite == "1"
AppId={{7A5FDA9C-4B27-4D80-9B4B-C3E1D42F9A01}
#else
AppId={{7A5FDA9C-4B27-4D80-9B4B-C3E1D42F9A02}
#endif
AppName={#MyApp}
AppVersion={#MyVer}
AppVerName={#MyApp} v{#MyVer}{#Edition}
AppPublisher=水星（非商用 · 个人使用）
DefaultDirName={autopf}\{#MyApp}{#Edition}
DefaultGroupName={#MyApp}{#Edition}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SourceDir={#RepoDir}
OutputDir=发布包
OutputBaseFilename={#OutName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=installer\app.ico
UninstallDisplayIcon={app}\app.ico
DisableWelcomePage=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："

[Files]
; ── 代码与运行时：升级即覆盖（新版本代码必须生效） ──
; 显式列出各顶层子目录/文件，独独把「水星配音数据」跳过——它下面另有规则保护。
Source: "{#SrcDir}\python\*";  DestDir: "{app}\python";  Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SrcDir}\source\*";  DestDir: "{app}\source";  Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#SrcDir}\server\*";  DestDir: "{app}\server";  Flags: recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\ffmpeg.exe";  DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\启动.bat";      DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\首次使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#SrcDir}\版本.txt";      DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 图标随包安装，供快捷方式引用
Source: "installer\app.ico"; DestDir: "{app}"; Flags: ignoreversion

; ── 用户成果目录（水星配音数据）：**只在目标缺失时释放种子** ──
; · onlyifdoesntexist：升级时若同名文件已存在（=用户已下载/克隆），跳过不覆盖；
;   新增的种子文件（新版本预置的字体/whisper 模型清单等）会补进去。
; · uninsneveruninstall：卸载时不删——用户已下的组件、音色库、克隆音频、成片
;   一律保留；卸载完这个目录仍在，下次重装能秒接。
; 打包时用户可能没这个目录（首装或空跑），加 skipifsourcedoesntexist 兜底。
Source: "{#SrcDir}\水星配音数据\*"; DestDir: "{app}\水星配音数据"; \
    Flags: recursesubdirs createallsubdirs onlyifdoesntexist uninsneveruninstall skipifsourcedoesntexist

[InstallDelete]
; 清掉旧版本残留的 pyc 缓存，避免旧字节码顶掉新源码（升级场景）；数据目录不动。
Type: filesandordirs; Name: "{app}\source\dub_align_studio\__pycache__"
Type: filesandordirs; Name: "{app}\source\integrated_workbench\__pycache__"

[UninstallDelete]
; 卸载时把安装器写的空「水星配音数据」壳目录删掉；有内容（用户成果）时 Windows
; 不会真删非空目录，自然保留——组合 uninsneveruninstall 达到"用户数据永远不删"。
Type: dirifempty; Name: "{app}\水星配音数据"

[Icons]
Name: "{group}\{#MyApp}"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Comment: "启动 {#MyApp}（浏览器自动打开）"
Name: "{group}\卸载 {#MyApp}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyApp}"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\启动.bat"; Description: "立即启动 {#MyApp}"; WorkingDir: "{app}"; Flags: shellexec postinstall skipifsilent nowait

[Messages]
#if Lite == "1"
WelcomeLabel2=即将安装 [name/ver]（轻量云配版）。%n%n配音走云 GPU（装好后在「设置-云配音」填云地址+API Key、引擎选 dots.tts 云GPU 远程）；本机无需独立显卡，集成显卡即可，渲染所需 ffmpeg 已随包。%n%n升级安装说明：安装器**只覆盖代码与运行时**；「水星配音数据」目录下的组件、音色库、克隆音频、成片、字体等已下载/生成的内容一律**保留**，不会因重装丢失。
#else
WelcomeLabel2=即将安装 [name/ver]（整合离线版，自带运行环境与模型）。%n%n注意：本机需 NVIDIA 显卡（配音走 CUDA）；渲染成片还需 ffmpeg。安装体量较大，请预留磁盘空间。%n%n升级安装说明：安装器**只覆盖代码与运行时**；「水星配音数据」目录下的组件、音色库、克隆音频、成片、字体等已下载/生成的内容一律**保留**，不会因重装丢失。
#endif

[Code]
{ 升级检测：读注册表里同 AppId 的旧版本；有就在准备页给出"数据保留"提示。 }
function IsUpgrade(): Boolean;
var
  OldVersion: String;
  Key: String;
begin
  Result := False;
  Key := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1';
  if RegQueryStringValue(HKLM, Key, 'DisplayVersion', OldVersion) then
    Result := True
  else if RegQueryStringValue(HKCU, Key, 'DisplayVersion', OldVersion) then
    Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsUpgrade() then
    MsgBox('检测到已安装旧版本，本次为升级安装。' + #13#10 + #13#10 +
           '「水星配音数据」目录下的以下内容将保留原样，不会被覆盖：' + #13#10 +
           ' · 已下载的组件（whisper 模型、字体等）' + #13#10 +
           ' · 音色库、克隆音频' + #13#10 +
           ' · 生成的成片、音效素材' + #13#10 + #13#10 +
           '仅代码与运行时会覆盖到新版本。',
           mbInformation, MB_OK);
end;
