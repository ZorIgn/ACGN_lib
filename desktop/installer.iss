#ifndef AppVersion
  #define AppVersion "0.4.0"
#endif
#ifndef PackageRoot
  #define PackageRoot "..\dist\ACGLib"
#endif

[Setup]
AppId={{AB9EC567-FB1F-43A7-9828-58EB51129BBE}
AppName=ACGLib 私人书架
AppVersion={#AppVersion}
AppPublisher=ZorIgn
AppPublisherURL=https://github.com/ZorIgn/ACGN_lib
AppSupportURL=https://github.com/ZorIgn/ACGN_lib/issues
DefaultDirName={localappdata}\Programs\ACGLib
DefaultGroupName=ACGLib
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=ACGLib-{#AppVersion}-Windows-Setup
SetupIconFile=..\src\static\img\acglib.ico
UninstallDisplayIcon={app}\ACGLib.exe
LicenseFile=..\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Files]
Source: "{#PackageRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,*.pyc,__pycache__\*,src\db\*,src\staticfiles\*"

[Icons]
Name: "{group}\ACGLib"; Filename: "{app}\ACGLib.exe"
Name: "{autodesktop}\ACGLib"; Filename: "{app}\ACGLib.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\ACGLib.exe"; Description: "Open ACGLib"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\ACGLib.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopACGLib"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\ACGLib.exe')) then
    if not Exec(ExpandConstant('{app}\ACGLib.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      Result := 'Please close ACGLib before continuing.';
end;
