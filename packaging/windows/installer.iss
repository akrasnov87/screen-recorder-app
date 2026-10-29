; ============================================================
;  Inno Setup скрипт для Screen Recorder & Transcriber
; ============================================================
;
; Требования:
;   1. Установлен Inno Setup 6+   https://jrsoftware.org/isinfo.php
;   2. Собрана папка dist\ScreenRecorder\ (см. build_win.ps1)
;   3. app.ico лежит в packaging\windows\
;
; Сборка:
;   iscc.exe packaging\windows\installer.iss
;
; Результат:
;   dist\installer\ScreenRecorder-<версия>-setup.exe
;
; ВНИМАНИЕ: версию нужно синхронизировать с src\__init__.py.
; Сам Inno Setup не умеет читать Python-файлы, поэтому версия
; задаётся здесь вручную (или через CI-параметр /DAppVersion).
; ============================================================

#ifndef AppVersion
  #define AppVersion "1.13.15"
#endif

#define AppName        "Screen Recorder"
#define AppPublisher   "Screen Recorder"
#define AppExeName     "ScreenRecorder.exe"
#define SourceDir      "..\..\dist\ScreenRecorder"

[Setup]
AppId={{8C4B7E21-3F5A-4D88-9C21-5D8E7A6B1234}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\..\dist\installer
OutputBaseFilename=ScreenRecorder-{#AppVersion}-setup
SetupIconFile=app.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\{#AppExeName}
MinVersion=10.0

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; \
    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[Code]
// Проверяем, что ffmpeg доступен в PATH. Если нет — предупреждаем.
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  FFmpegFound: Boolean;
begin
  Result := True;

  FFmpegFound := Exec(
    'cmd.exe', '/c where ffmpeg >nul 2>nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) and (ResultCode = 0);

  if not FFmpegFound then
  begin
    if MsgBox(
      'ffmpeg не найден в PATH.' + #13#10 + #13#10 +
      'Конвертация видео и автосжатие медиа будут недоступны. ' +
      'Скачайте ffmpeg с https://ffmpeg.org/download.html ' +
      'и добавьте папку с ffmpeg.exe в PATH.' + #13#10 + #13#10 +
      'Продолжить установку?',
      mbConfirmation, MB_YESNO
    ) = IDNO then
      Result := False;
  end;
end;