#define MyAppName "Dealer Inventory Sync"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Rocky Mountain ATV"
#define MyAppExeName "DealerInventorySync.exe"

[Setup]
AppId={{D7265240-E684-4A90-98E9-4B54366932C5}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\DealerInventorySync
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=DealerInventorySync-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DealerInventorySync"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue

[Icons]
Name: "{group}\View Sync Log"; Filename: "notepad.exe"; Parameters: """{app}\dealer_sync.log"""
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start Dealer Inventory Sync"; Flags: nowait runhidden

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/IM {#MyAppExeName} /F"; Flags: runhidden; RunOnceId: "StopAgent"

[Code]
var
  SettingsPage: TInputQueryWizardPage;

function JsonEscape(Value: String): String;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  StringChangeEx(Value, #13#10, '\n', True);
  StringChangeEx(Value, #10, '\n', True);
  Result := Value;
end;

procedure InitializeWizard;
begin
  SettingsPage :=
    CreateInputQueryPage(
      wpSelectDir,
      'Connect this dealer',
      'Enter the connection details supplied by DealerOps.',
      'The agent watches the selected folder and securely sends new or changed Excel files to the dashboard.'
    );
  SettingsPage.Add('Dashboard upload URL:', False);
  SettingsPage.Add('Dealer ID:', False);
  SettingsPage.Add('Dealer API key:', True);
  SettingsPage.Add('Inventory folder to watch:', False);
  SettingsPage.Values[0] := 'https://your-app.replit.app/api/upload';
  SettingsPage.Values[3] := ExpandConstant('{userdocs}\Dealer Inventory');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  DealerId: Integer;
begin
  Result := True;
  if CurPageID <> SettingsPage.ID then
    Exit;

  if Pos('https://', Lowercase(Trim(SettingsPage.Values[0]))) <> 1 then
  begin
    MsgBox('The dashboard upload URL must begin with https://.', mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if (not TryStrToInt(Trim(SettingsPage.Values[1]), DealerId)) or (DealerId < 1) then
  begin
    MsgBox('Enter a valid positive Dealer ID.', mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if Trim(SettingsPage.Values[2]) = '' then
  begin
    MsgBox('Enter the dealer API key supplied by DealerOps.', mbError, MB_OK);
    Result := False;
    Exit;
  end;

  if Trim(SettingsPage.Values[3]) = '' then
  begin
    MsgBox('Choose an inventory folder to watch.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Config: String;
  DealerId: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  DealerId := StrToInt(Trim(SettingsPage.Values[1]));
  ForceDirectories(Trim(SettingsPage.Values[3]));
  Config :=
    '{' + #13#10 +
    '  "watch_folder": "' + JsonEscape(Trim(SettingsPage.Values[3])) + '",' + #13#10 +
    '  "api_url": "' + JsonEscape(Trim(SettingsPage.Values[0])) + '",' + #13#10 +
    '  "api_key": "' + JsonEscape(Trim(SettingsPage.Values[2])) + '",' + #13#10 +
    '  "dealer_id": ' + IntToStr(DealerId) + ',' + #13#10 +
    '  "fallback_interval_minutes": 15,' + #13#10 +
    '  "settings_poll_interval_minutes": 5,' + #13#10 +
    '  "log_file": "dealer_sync.log",' + #13#10 +
    '  "state_file": "dealer_sync_state.json",' + #13#10 +
    '  "request_timeout_seconds": 60,' + #13#10 +
    '  "retry_attempts": 5,' + #13#10 +
    '  "retry_base_delay_seconds": 5,' + #13#10 +
    '  "file_stability_checks": 3,' + #13#10 +
    '  "file_stability_delay_seconds": 2' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(ExpandConstant('{app}\config.json'), Config, False);
end;