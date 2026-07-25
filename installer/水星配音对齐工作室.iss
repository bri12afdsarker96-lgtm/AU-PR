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

[Setup]
AppName={#MyApp}
AppVersion={#MyVer}
AppVerName={#MyApp} v{#MyVer}
AppPublisher=水星（非商用 · 个人使用）
DefaultDirName={autopf}\{#MyApp}
DefaultGroupName={#MyApp}
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SourceDir={#RepoDir}
OutputDir=发布包
OutputBaseFilename=水星配音对齐工作室_安装程序_v{#MyVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=installer\app.ico
UninstallDisplayIcon={app}\app.ico
DisableWelcomePage=no

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："

[Files]
; 整合离线包整目录（含 python\ source\ 水星配音数据\ hf_cache\ 启动.bat 等）
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; 图标随包安装，供快捷方式引用
Source: "installer\app.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyApp}"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Comment: "启动 {#MyApp}（浏览器自动打开）"
Name: "{group}\卸载 {#MyApp}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyApp}"; Filename: "{app}\启动.bat"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\启动.bat"; Description: "立即启动 {#MyApp}"; WorkingDir: "{app}"; Flags: shellexec postinstall skipifsilent nowait

[Messages]
WelcomeLabel2=即将安装 [name/ver]（整合离线版，自带运行环境与模型）。%n%n注意：本机需 NVIDIA 显卡（配音走 CUDA）；渲染成片还需 ffmpeg。安装体量较大，请预留磁盘空间。
