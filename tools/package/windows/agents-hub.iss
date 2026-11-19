; Setup.exe of the Windows app (installer spec S6). Compiled by tools/package/windows/setup_exe.py:
;   ISCC /DSource=<the app folder of build.py windows-x64> /DVersion=1.2.3 /DOutputDir=<dir> /DOutputName=<name>
; For the current user, without administrator rights and without asking where: %LOCALAPPDATA%\Programs\Agents Hub.
; The hub itself is set up by the app's app.py (install — after the files, stop — before replacing them, uninstall),
; which runs the code's deploy/windows/install.ps1: the scheduler task stays registered in one place.

[Setup]
; never change the AppId: Windows knows an installed app by it (upgrades, "Apps & features")
AppId={{33EF1A1E-FFF1-4BCF-BBE8-91FC0FFDD478}
AppName=Hub by Icons8
AppVersion={#Version}
AppVerName=Hub by Icons8 {#Version}
AppPublisher=Hub by Icons8
VersionInfoVersion={#Version}
DefaultDirName={autopf}\Agents Hub
UsePreviousAppDir=yes
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableReadyPage=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
SetupIconFile={#Source}\app.ico
UninstallDisplayIcon={app}\app.ico
UninstallDisplayName=Hub by Icons8
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
; English only: the setup comes before the hub knows its owner's language (the page then speaks it)
ShowLanguageDialog=no
SetupLogging=yes
; the hub's processes are stopped by the hub itself (PrepareToInstall waits for them): Restart Manager closing its Python
; silently left the scheduler task Running on an orphaned cmd.exe, and the new hub never started (app spec, probe A0)
CloseApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
en.Starting=Starting the hub…
en.Failed=The hub did not start. What happened is written in%n%1%n%nHub by Icons8 in the Start menu tries again.
en.Open=Open Hub by Icons8
en.Foreign=A hub is already installed on this computer (from git or by the install script), its data are in%n%1%n%nThe app installs only where there is no hub yet — nothing has been changed. Keep using that hub, or remove it together with its data (hub uninstall --purge) or move that folder away, and run this setup again.
en.Held=The hub still runs programs of %1 and did not stop; nothing has been changed. Run this setup again in a minute.
en.AskPurge=Also delete the hub's data — the journal, the conversations of sessions and the settings?%n%nNo — they stay, and installing Hub by Icons8 again picks them up.

[InstallDelete]
; an upgrade: what the previous build had and this one does not must not stay (the hub is stopped in PrepareToInstall)
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\code-seed"
; the Start menu entry of earlier builds, before it was named as on Linux and in the one-line install
Type: files; Name: "{userprograms}\Agents Hub.lnk"

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Hub by Icons8"; Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\app.py"" open"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; Comment: "Hub by Icons8"

[Run]
Filename: "{app}\runtime\python\pythonw.exe"; Parameters: """{app}\app.py"" open"; WorkingDir: "{app}"; Description: "{cm:Open}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
; what the app made itself: hard links and msys links of links.json, Python's bytecode
Type: filesandordirs; Name: "{app}"

[Code]
function AppPython(): String;
begin
  Result := ExpandConstant('{app}\runtime\python\python.exe');
end;

function AppPythonW(): String;
begin
  Result := ExpandConstant('{app}\runtime\python\pythonw.exe');
end;

function AppScript(): String;
begin
  Result := '"' + ExpandConstant('{app}\app.py') + '"';
end;

function HubState(): String;
begin
  Result := ExpandConstant('{localappdata}\hub-core');
end;

function PageAnswers(): Boolean;
var
  Request: Variant;
begin
  try
    Request := CreateOleObject('WinHttp.WinHttpRequest.5.1');
    Request.SetTimeouts(1000, 1000, 1000, 1000);
    Request.Open('GET', 'http://127.0.0.1:8787/', False);
    Request.Send('');
    Result := True;   { any answer: a hub is there }
  except
    Result := False;
  end;
end;

function TaskExists(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\schtasks.exe'), '/Query /TN "Agents Hub"', '', SW_HIDE, ewWaitUntilTerminated,
                 Code) and (Code = 0);
end;

{ The app's own hub has a layer of code in layers\<name>\ (its MANIFEST.json). <state>\code is a junction the user made,
  and Setup.exe's own process cannot follow it (RedirectionGuard, "untrusted mount point": FileExists(code\MANIFEST.json)
  is False for an installed app, and an upgrade was refused as a foreign hub, the Windows stand 07.11): the layers are
  looked at by their own folders. }
function OwnLayer(): Boolean;
var
  Found: TFindRec;
begin
  Result := False;
  if FindFirst(HubState() + '\layers\*', Found) then
  try
    repeat
      if FileExists(HubState() + '\layers\' + Found.Name + '\MANIFEST.json') then
      begin
        Result := True;
        Break;
      end;
    until not FindNext(Found);
  finally
    FindClose(Found);
  end;
end;

{ installer spec 13-6: the app does not go onto a hub it cannot take over — it stops before any change. The app's own
  hub (installed before, or its data kept by an uninstall) has its code in the state folder (a clone of the seed, or a
  code layer: layers\); a hub of deploy\windows\install.ps1 (from git) has its launcher there — app.py install asks
  "Move / Stay on the clone / Later" (app spec §8) and changes nothing unless told to move. Another hub has its data
  there without either, or its scheduler task, or its page. }
function ForeignHub(): Boolean;
begin
  { the app's own: its code a clone (S3), or a code layer: behind the pointer (app spec 3.1: the shell's record) or in
    a layer's own folder }
  if DirExists(HubState() + '\code\.git') or FileExists(HubState() + '\layers\shell.json')
     or FileExists(HubState() + '\code\MANIFEST.json') or OwnLayer()
     or DirExists(HubState() + '\layers') or FileExists(HubState() + '\hub-up.cmd') then
    Result := False
  else
    Result := FileExists(HubState() + '\journal.jsonl') or FileExists(HubState() + '\state.json') or TaskExists()
              or PageAnswers();
end;

function InitializeSetup(): Boolean;
begin
  Result := not ForeignHub();
  if not Result then
    SuppressibleMsgBox(FmtMessage(CustomMessage('Foreign'), [HubState()]), mbError, MB_OK, IDOK);
end;

{ a process still runs a program of the app folder (the hub's Python, its Node): exit code 3 of the check. Any other
  answer (PowerShell missing, a path it does not parse) is "free" — as before this check }
function AppBusy(): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                 '-NoProfile -NonInteractive -Command "$a = ''' + ExpandConstant('{app}') + '\''; ' +
                 'if (Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and ' +
                 '$_.ExecutablePath.StartsWith($a, ''OrdinalIgnoreCase'') }) { exit 3 } else { exit 0 }"',
                 '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 3);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code, I: Integer;
begin
  Result := '';
  { an upgrade: the hub runs on this app's files — stop it before they are replaced }
  if FileExists(AppPython()) and FileExists(ExpandConstant('{app}\app.py')) then
  begin
    Exec(AppPython(), AppScript() + ' stop', ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
    { `hub stop` asks; the files are free only when every process of the app is gone. One that stays: nothing is
      replaced (the setup ends before any change) and the old app is whole — the hub's watchdog starts it again }
    I := 0;
    while AppBusy() and (I < 90) do
    begin
      Sleep(1000);
      I := I + 1;
    end;
    if AppBusy() then
      Result := FmtMessage(CustomMessage('Held'), [ExpandConstant('{app}')]);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Args: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('Starting');
    { pythonw shown normally: no console, and the move dialog of a hub from git (app spec §8) is a window of its own;
      a silent install asks nothing }
    Args := ' install';
    if WizardSilent() then
      Args := Args + ' --silent';
    if not Exec(AppPythonW(), AppScript() + Args, ExpandConstant('{app}'), SW_SHOWNORMAL, ewWaitUntilTerminated, Code)
       or (Code <> 0) then
      SuppressibleMsgBox(FmtMessage(CustomMessage('Failed'), [ExpandConstant('{localappdata}\hub-core\logs\install.log')]),
                         mbError, MB_OK, IDOK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Args: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Args := ' uninstall';
    { the data stays unless the person says otherwise; a silent uninstall keeps it }
    if SuppressibleMsgBox(CustomMessage('AskPurge'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      Args := Args + ' --purge';
    Exec(AppPython(), AppScript() + Args, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  end;
end;
