; dogani 설치 파일 (Inno Setup 6). build.bat 이 dist\dogani 를 만든 뒤 iscc 로 컴파일 → dist\dogani-setup.exe
; 사용자 단위 설치(관리자 권한 없음). 설정·데이터(%APPDATA%\dogani)는 제거해도 남김
#ifndef AppVer
  #define AppVer "dev"
#endif

[Setup]
AppId={{6F4D2B1E-7C3A-4E8F-9B21-D0A9C5E3F781}
AppName=dogani
AppVersion={#AppVer}
AppVerName=dogani {#AppVer}
AppPublisher=dogani
DefaultDirName={localappdata}\Programs\dogani
DefaultGroupName=dogani
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=dogani-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\dogani.exe
UninstallDisplayName=dogani
; 실행 중이면(같은 mutex) 닫으라고 안내
AppMutex=DoganiMutex
CloseApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "autostart"; Description: "Windows 시작 시 자동 실행"
Name: "desktopicon"; Description: "바탕화면 바로가기"; Flags: unchecked

[Files]
Source: "dist\dogani\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 예전 빌드의 남은 파일이 섞이지 않게 _internal 은 매번 새로
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\dogani"; Filename: "{app}\dogani.exe"
Name: "{userdesktop}\dogani"; Filename: "{app}\dogani.exe"; Tasks: desktopicon

[Registry]
; 앱의 트레이 '자동 실행' 과 같은 값(HKCU Run\dogani)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "dogani"; ValueData: """{app}\dogani.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\dogani.exe"; Description: "dogani 실행"; Flags: nowait postinstall skipifsilent

[Code]
// 앱 안 자동 업데이트(updater.setup_command 가 /RELAUNCH=1 을 줌)는 런처가 먼저 꺼짐 → 설치가 끝나면 다시 띄움.
// [Run] 은 성공했을 때만 돌아서, 실패하면(파일 잠김 등) dogani 가 안 켜진 채 끝남 → 성공·실패 모두 지나는 여기서.
// 실패하면 Inno 가 되돌려 놓으므로 예전 exe 가 뜸
procedure DeinitializeSetup();
var
  Code: Integer;
begin
  if ExpandConstant('{param:RELAUNCH|0}') <> '1' then
    Exit;
  try
    Exec(ExpandConstant('{app}\dogani.exe'), '', '', SW_SHOWNORMAL, ewNoWait, Code);
  except
    Exec(ExpandConstant('{localappdata}\Programs\dogani\dogani.exe'), '', '', SW_SHOWNORMAL, ewNoWait, Code);
  end;
end;

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im dogani.exe /f"; Flags: runhidden; RunOnceId: "killdogani"
