; Inno Setup script — per-user install, no admin prompt.
; Build with:  iscc BananaStand.iss   (from the repo root, after PyInstaller)

#define AppName "The Banana Stand"
; CI passes the release tag: iscc /DAppVersion=1.21.10 BananaStand.iss
#ifndef AppVersion
  #define AppVersion "1.21.17"
#endif
#define AppExe "BananaStand.exe"

[Setup]
AppId={{8E4C1C7A-6B2F-4C63-9D2A-6F1B0C7E4A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=The Banana Stand
DefaultDirName={localappdata}\Programs\BananaStand
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=BananaStandSetup
SetupIconFile=assets\banana.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; An update over the same AppId must replace the program folder cleanly, but it must
; never touch %LOCALAPPDATA%\BananaStand, where the ledger and the key live.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start with Windows (recommended — backups and sync only run while it is open)"; GroupDescription: "Startup:"

[Files]
Source: "dist\BananaStand\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "SETUP-WINDOWS.txt"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; PyInstaller keeps its modules under _internal. Removing it before an update
; prevents stale modules from an older build being loaded alongside the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Setup notes"; Filename: "{app}\SETUP-WINDOWS.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "BananaStand"; ValueData: """{app}\{#AppExe}"" --no-browser"; Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the program files are removed. User data stays in %LOCALAPPDATA%\BananaStand.

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { The tray app has no main window, so Inno's Restart Manager can wait on it
    indefinitely and the install appears to hang. Close it before replacing files;
    SQLite with WAL is crash-safe, and the installer restarts it afterwards. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM BananaStand.exe /F', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
