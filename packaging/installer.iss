#define MyAppName "墨衡 MOHENG"
#define MyAppPublisher "MOHENG"
#define MyAppExeName "Tideguard.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.4.0"
#endif
#define MyAppSourceDir GetEnv("TIDEGUARD_PACKAGE_SOURCE")
#if MyAppSourceDir == ""
  #define MyAppSourceDir "output\Tideguard"
#endif
#define MyWebView2Bootstrapper GetEnv("TIDEGUARD_WEBVIEW2_BOOTSTRAPPER")
#if MyWebView2Bootstrapper == ""
  #error TIDEGUARD_WEBVIEW2_BOOTSTRAPPER must point to the Microsoft-signed Evergreen Bootstrapper
#endif

[Setup]
AppId={{2D2663B4-03DE-4F3D-BC77-12556DEBA51F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
LicenseFile=..\LICENSE
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/mikutea/tideguard-okx-demo
AppSupportURL=https://github.com/mikutea/tideguard-okx-demo/issues
AppUpdatesURL=https://github.com/mikutea/tideguard-okx-demo/releases
DefaultDirName={localappdata}\Programs\Tideguard
DefaultGroupName=墨衡 MOHENG
UsePreviousGroup=no
UsePreviousTasks=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir=output
OutputBaseFilename=Moheng-Setup-{#MyAppVersion}
SetupIconFile=..\assets\brand\moheng.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
Uninstallable=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
AppMutex=Local\Tideguard.Desktop.2d2663b4-03de-4f3d-bc77-12556deba51f,Local\Tideguard.Credentials.2d2663b4-03de-4f3d-bc77-12556deba51f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked
Name: "autostart"; Description: "登录 Windows 后启动墨衡后台研究服务（系统策略允许时）"; GroupDescription: "长期运行："; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#MyWebView2Bootstrapper}"; DestDir: "{tmp}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: deleteafterinstall; Check: not IsWebView2Installed; AfterInstall: InstallWebView2

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{userprograms}\Tideguard"
Type: files; Name: "{autodesktop}\Tideguard.lnk"
Type: files; Name: "{userstartup}\Tideguard 后台服务.lnk"
Type: files; Name: "{userstartup}\墨衡后台服务.lnk"

[Icons]
Name: "{group}\墨衡 MOHENG"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\墨衡凭证管理"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--credentials"
Name: "{group}\启动墨衡后台服务"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--daemon"; WorkingDir: "{app}"
Name: "{group}\停止墨衡后台服务"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop-daemon"
Name: "{autodesktop}\墨衡 MOHENG"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Tideguard.BackgroundService"; ValueData: "{code:GetAutostartCommand}"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--daemon"; Description: "启动墨衡后台研究服务"; Flags: nowait runhidden postinstall skipifsilent; Tasks: autostart
Filename: "{app}\{#MyAppExeName}"; Description: "启动墨衡 MOHENG"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--stop-daemon"; Flags: runhidden waituntilterminated; RunOnceId: "StopTideguardDaemon"

[Code]
const
  WebView2ClientId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2Version(RootKey: Integer; Subkey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, Subkey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0');
end;

function IsWebView2Installed(): Boolean;
begin
  Result :=
    HasWebView2Version(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2ClientId) or
    HasWebView2Version(HKCU,
      'Software\Microsoft\EdgeUpdate\Clients\' + WebView2ClientId);
end;

procedure InstallWebView2();
var
  ResultCode: Integer;
begin
  if not Exec(
    ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
    '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) then
    RaiseException('无法启动 Microsoft Edge WebView2 Runtime 安装程序。');
  if not IsWebView2Installed() then
    RaiseException(Format(
      'Microsoft Edge WebView2 Runtime 安装失败（退出代码 %d）。', [ResultCode]
    ));
end;

function StopExistingDaemon(): Boolean;
var
  ResultCode: Integer;
  ExePath: String;
begin
  Result := True;
  ExePath := ExpandConstant('{app}\{#MyAppExeName}');
  if FileExists(ExePath) then
  begin
    if not Exec(ExePath, '--stop-daemon', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) then
      Result := False
    else if ResultCode <> 0 then
      Result := False;
    if Result then
      Sleep(750);
  end;
end;

function GetAutostartCommand(Param: String): String;
begin
  Result := '"' + ExpandConstant('{app}\{#MyAppExeName}') + '" --daemon';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if StopExistingDaemon() then
    Result := ''
  else
    Result := '无法停止现有墨衡后台服务，请稍后重试。';
end;

function InitializeUninstall(): Boolean;
begin
  StopExistingDaemon();
  Result := True;
end;
