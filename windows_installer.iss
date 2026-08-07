#define MyAppName "Twitch Channel Points Miner"
#define MyAppPublisher "Twitch Channel Points Miner"
#define MyAppExeName "TwitchChannelPointsMiner.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8D7522D0-35E5-45A8-8F5E-E46049123B3F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\TwitchChannelPointsMiner
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=TwitchChannelPointsMiner-{#MyAppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "configure"; Description: "Prefill a new configuration"; GroupDescription: "First run:"; Flags: checkedonce

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.example.py"; DestDir: "{app}\config"; DestName: "config.py"; Flags: onlyifdoesntexist; AfterInstall: CustomizeStarterConfig

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
Filename: "https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3#configuration-file"; Description: "Open the configuration guide"; Flags: shellexec postinstall skipifsilent unchecked

[Code]
var
  ConfigurePage: TInputQueryWizardPage;

function EscapePythonString(Value: String): String;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  Result := Value;
end;

function PythonStreamerList(Value: String): String;
var
  Channels: TArrayOfString;
  I: Integer;
  Channel: String;
begin
  StringChangeEx(Value, ',', #13#10, True);
  Channels := SplitString(Value, #13#10);
  Result := '';
  for I := 0 to GetArrayLength(Channels) - 1 do
  begin
    Channel := Trim(Channels[I]);
    if Channel <> '' then
    begin
      if Result <> '' then
        Result := Result + ', ';
      Result := Result + '"' + EscapePythonString(Channel) + '"';
    end;
  end;
end;

procedure CustomizeStarterConfig;
var
  ConfigPath: String;
  ConfigBytes: AnsiString;
  ConfigText: String;
  Username: String;
  Streamers: String;
  StartAt: Integer;
  EndAt: Integer;
begin
  ConfigPath := ExpandConstant('{app}\config\config.py');
  if not LoadStringFromFile(ConfigPath, ConfigBytes) then
    Exit;
  ConfigText := String(ConfigBytes);

  Username := Trim(ConfigurePage.Values[0]);
  if Username <> '' then
    StringChangeEx(ConfigText, '"your-twitch-username"',
      '"' + EscapePythonString(Username) + '"', True);

  Streamers := PythonStreamerList(ConfigurePage.Values[1]);
  if Streamers <> '' then
  begin
    StartAt := Pos('STREAMERS = [', ConfigText);
    if StartAt > 0 then
    begin
      EndAt := PosEx('    ]', ConfigText, StartAt);
      if EndAt > 0 then
        ConfigText := Copy(ConfigText, 1, StartAt - 1) +
          'STREAMERS = [' + Streamers + ']' +
          Copy(ConfigText, EndAt + Length('    ]'), MaxInt);
    end;
  end;

  ConfigBytes := AnsiString(ConfigText);
  SaveStringToFile(ConfigPath, ConfigBytes, False);
end;

procedure InitializeWizard;
begin
  ConfigurePage := CreateInputQueryPage(wpSelectTasks,
    'Configure the miner',
    'Optionally prefill the initial configuration',
    'These values are used only when no configuration already exists. ' +
    'You can change every setting later in config\config.py.');
  ConfigurePage.Add('Twitch username:', False);
  ConfigurePage.Add('Channels to watch (comma-separated):', False);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = ConfigurePage.ID) and
    (not WizardIsTaskSelected('configure'));
end;
