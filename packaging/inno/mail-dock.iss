#ifndef AppVersion
  #error AppVersion must be supplied from mail_dock.__version__ by tools/build_windows.ps1
#endif

[Setup]
AppId={{8B45BC42-8F32-4F13-A7A6-C6409D0EAAB1}
AppName=mail-dock
AppVersion={#AppVersion}
AppPublisher=mail-dock contributors
AppPublisherURL=https://github.com/sho-kawasaki-dd/mail-dock
DefaultDirName={autopf}\mail-dock
DefaultGroupName=mail-dock
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE
UninstallDisplayName=mail-dock
UninstallDisplayIcon={app}\mail-dock.exe
OutputDir=..\..\dist
OutputBaseFilename=mail-dock-{#AppVersion}-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\..\dist\mail-dock\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\mail-dock"; Filename: "{app}\mail-dock.exe"
Name: "{group}\License materials"; Filename: "{app}\_internal\licenses"
Name: "{autodesktop}\mail-dock"; Filename: "{app}\mail-dock.exe"; Tasks: desktopicon

[CustomMessages]
english.UninstallSettingsPrompt=Remove mail-dock settings and application logs for this Windows user?%n%nMail storage data and credentials saved in Windows Credential Manager (keyring) will not be deleted.
english.UninstallSettingsSkipped=Settings and application logs were left in place. Storage data and keyring credentials are not removed by the uninstaller.
english.UninstallSettingsUnsafe=Settings and application logs were left in place because their location could not be verified as safe. Storage data and keyring credentials were not touched.
english.UninstallOtherUsers=This is a system-wide installation. Settings for each Windows user are left in place; storage data and keyring credentials are not removed.
japanese.UninstallSettingsPrompt=このWindowsユーザーのmail-dock設定とアプリログを削除しますか？%n%nストレージルート上のメールデータと、Windows資格情報マネージャー（keyring）に保存された資格情報は削除されません。
japanese.UninstallSettingsSkipped=設定とアプリログはそのまま残しました。ストレージルート上のデータとkeyringの資格情報はアンインストーラーから削除されません。
japanese.UninstallSettingsUnsafe=保存先の安全性を確認できなかったため、設定とアプリログはそのまま残しました。ストレージデータとkeyringには触れていません。
japanese.UninstallOtherUsers=全ユーザー向けインストールです。各Windowsユーザーの設定はそのまま残ります。ストレージデータとkeyringの資格情報は削除されません。

[Code]
const
  INVALID_FILE_ATTRIBUTES = $FFFFFFFF;
  ERROR_FILE_NOT_FOUND = 2;
  ERROR_PATH_NOT_FOUND = 3;

function GetFileAttributesW(FileName: string): LongWord;
  external 'GetFileAttributesW@kernel32.dll stdcall';
function GetLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';

function IsJsonWhitespace(C: Char): Boolean;
begin
  Result := (C = ' ') or (C = #9) or (C = #10) or (C = #13);
end;

procedure SkipJsonWhitespace(const Json: string; var Position: Integer);
begin
  while (Position <= Length(Json)) and IsJsonWhitespace(Json[Position]) do
    Position := Position + 1;
end;

function TryReadHex4(const Json: string; var Position: Integer; var CodePoint: Integer): Boolean;
var
  I, Digit: Integer;
  C: Char;
begin
  Result := False;
  if Position + 3 > Length(Json) then
    exit;
  CodePoint := 0;
  for I := 0 to 3 do begin
    C := Json[Position + I];
    if (C >= '0') and (C <= '9') then
      Digit := Ord(C) - Ord('0')
    else if (C >= 'a') and (C <= 'f') then
      Digit := Ord(C) - Ord('a') + 10
    else if (C >= 'A') and (C <= 'F') then
      Digit := Ord(C) - Ord('A') + 10
    else
      exit;
    CodePoint := CodePoint * 16 + Digit;
  end;
  Position := Position + 4;
  Result := True;
end;

function TryReadJsonString(const Json: string; var Position: Integer; var Value: string): Boolean;
var
  C: Char;
  CodePoint, LowSurrogate: Integer;
begin
  Result := False;
  Value := '';
  if (Position > Length(Json)) or (Json[Position] <> '"') then
    exit;
  Position := Position + 1;
  while Position <= Length(Json) do begin
    C := Json[Position];
    Position := Position + 1;
    if C = '"' then begin
      Result := True;
      exit;
    end;
    if Ord(C) < 32 then
      exit;
    if C <> '\' then begin
      Value := Value + C;
      continue;
    end;
    if Position > Length(Json) then
      exit;
    C := Json[Position];
    Position := Position + 1;
    case C of
      '"': Value := Value + '"';
      '\': Value := Value + '\';
      '/': Value := Value + '/';
      'b': Value := Value + #8;
      'f': Value := Value + #12;
      'n': Value := Value + #10;
      'r': Value := Value + #13;
      't': Value := Value + #9;
      'u': begin
        if not TryReadHex4(Json, Position, CodePoint) then
          exit;
        if (CodePoint >= $D800) and (CodePoint <= $DBFF) then begin
          if (Position + 5 > Length(Json)) or (Json[Position] <> '\') or
             (Json[Position + 1] <> 'u') then
            exit;
          Position := Position + 2;
          if not TryReadHex4(Json, Position, LowSurrogate) or
             (LowSurrogate < $DC00) or (LowSurrogate > $DFFF) then
            exit;
          Value := Value + Chr(CodePoint) + Chr(LowSurrogate);
        end else if (CodePoint >= $DC00) and (CodePoint <= $DFFF) then
          exit
        else
          Value := Value + Chr(CodePoint);
      end;
    else
      exit;
    end;
  end;
end;

function TrySkipJsonValue(const Json: string; var Position: Integer): Boolean; forward;

function TrySkipJsonObject(const Json: string; var Position: Integer): Boolean;
var
  Key: string;
begin
  Result := False;
  Position := Position + 1;
  SkipJsonWhitespace(Json, Position);
  if (Position <= Length(Json)) and (Json[Position] = '}') then begin
    Position := Position + 1;
    Result := True;
    exit;
  end;
  while Position <= Length(Json) do begin
    if not TryReadJsonString(Json, Position, Key) then
      exit;
    SkipJsonWhitespace(Json, Position);
    if (Position > Length(Json)) or (Json[Position] <> ':') then
      exit;
    Position := Position + 1;
    if not TrySkipJsonValue(Json, Position) then
      exit;
    SkipJsonWhitespace(Json, Position);
    if Position > Length(Json) then
      exit;
    if Json[Position] = '}' then begin
      Position := Position + 1;
      Result := True;
      exit;
    end;
    if Json[Position] <> ',' then
      exit;
    Position := Position + 1;
    SkipJsonWhitespace(Json, Position);
    if (Position > Length(Json)) or (Json[Position] = '}') then
      exit;
  end;
end;

function TrySkipJsonArray(const Json: string; var Position: Integer): Boolean;
begin
  Result := False;
  Position := Position + 1;
  SkipJsonWhitespace(Json, Position);
  if (Position <= Length(Json)) and (Json[Position] = ']') then begin
    Position := Position + 1;
    Result := True;
    exit;
  end;
  while Position <= Length(Json) do begin
    if not TrySkipJsonValue(Json, Position) then
      exit;
    SkipJsonWhitespace(Json, Position);
    if Position > Length(Json) then
      exit;
    if Json[Position] = ']' then begin
      Position := Position + 1;
      Result := True;
      exit;
    end;
    if Json[Position] <> ',' then
      exit;
    Position := Position + 1;
    SkipJsonWhitespace(Json, Position);
    if (Position > Length(Json)) or (Json[Position] = ']') then
      exit;
  end;
end;

function TrySkipJsonNumber(const Json: string; var Position: Integer): Boolean;
var
  StartPosition: Integer;
begin
  Result := False;
  StartPosition := Position;
  if Json[Position] = '-' then
    Position := Position + 1;
  if Position > Length(Json) then
    exit;
  if Json[Position] = '0' then
    Position := Position + 1
  else if (Json[Position] >= '1') and (Json[Position] <= '9') then begin
    while (Position <= Length(Json)) and (Json[Position] >= '0') and
          (Json[Position] <= '9') do
      Position := Position + 1;
  end else
    exit;
  if (Position <= Length(Json)) and (Json[Position] = '.') then begin
    Position := Position + 1;
    if (Position > Length(Json)) or (Json[Position] < '0') or (Json[Position] > '9') then
      exit;
    while (Position <= Length(Json)) and (Json[Position] >= '0') and
          (Json[Position] <= '9') do
      Position := Position + 1;
  end;
  if (Position <= Length(Json)) and ((Json[Position] = 'e') or (Json[Position] = 'E')) then begin
    Position := Position + 1;
    if (Position <= Length(Json)) and ((Json[Position] = '+') or (Json[Position] = '-')) then
      Position := Position + 1;
    if (Position > Length(Json)) or (Json[Position] < '0') or (Json[Position] > '9') then
      exit;
    while (Position <= Length(Json)) and (Json[Position] >= '0') and
          (Json[Position] <= '9') do
      Position := Position + 1;
  end;
  Result := Position > StartPosition;
end;

function TrySkipJsonValue(const Json: string; var Position: Integer): Boolean;
var
  Token: string;
begin
  Result := False;
  SkipJsonWhitespace(Json, Position);
  if Position > Length(Json) then
    exit;
  case Json[Position] of
    '"': begin
      Token := '';
      Result := TryReadJsonString(Json, Position, Token);
    end;
    '{': Result := TrySkipJsonObject(Json, Position);
    '[': Result := TrySkipJsonArray(Json, Position);
    't': if Copy(Json, Position, 4) = 'true' then begin
      Position := Position + 4;
      Result := True;
    end;
    'f': if Copy(Json, Position, 5) = 'false' then begin
      Position := Position + 5;
      Result := True;
    end;
    'n': if Copy(Json, Position, 4) = 'null' then begin
      Position := Position + 4;
      Result := True;
    end;
    '-', '0'..'9': Result := TrySkipJsonNumber(Json, Position);
  end;
end;

function TryReadStringArray(const Json: string; var Position: Integer;
  var Values: TArrayOfString): Boolean;
var
  Value: string;
  Count: Integer;
begin
  Result := False;
  SetArrayLength(Values, 0);
  SkipJsonWhitespace(Json, Position);
  if (Position > Length(Json)) or (Json[Position] <> '[') then
    exit;
  Position := Position + 1;
  SkipJsonWhitespace(Json, Position);
  if (Position <= Length(Json)) and (Json[Position] = ']') then begin
    Position := Position + 1;
    Result := True;
    exit;
  end;
  Count := 0;
  while Position <= Length(Json) do begin
    if not TryReadJsonString(Json, Position, Value) then
      exit;
    SetArrayLength(Values, Count + 1);
    Values[Count] := Value;
    Count := Count + 1;
    SkipJsonWhitespace(Json, Position);
    if Position > Length(Json) then
      exit;
    if Json[Position] = ']' then begin
      Position := Position + 1;
      Result := True;
      exit;
    end;
    if Json[Position] <> ',' then
      exit;
    Position := Position + 1;
    SkipJsonWhitespace(Json, Position);
  end;
end;

function TryReadStorageCandidates(const Json: string; var Values: TArrayOfString): Boolean;
var
  Position: Integer;
  Key: string;
  FoundCandidates, Done: Boolean;
begin
  Result := False;
  SetArrayLength(Values, 0);
  Position := 1;
  SkipJsonWhitespace(Json, Position);
  if (Position > Length(Json)) or (Json[Position] <> '{') then
    exit;
  Position := Position + 1;
  FoundCandidates := False;
  Done := False;
  while not Done do begin
    SkipJsonWhitespace(Json, Position);
    if Position > Length(Json) then
      exit;
    if Json[Position] = '}' then begin
      Position := Position + 1;
      Done := True;
      continue;
    end;
    if not TryReadJsonString(Json, Position, Key) then
      exit;
    SkipJsonWhitespace(Json, Position);
    if (Position > Length(Json)) or (Json[Position] <> ':') then
      exit;
    Position := Position + 1;
    SkipJsonWhitespace(Json, Position);
    if Key = 'storage_root_candidates' then begin
      if FoundCandidates or not TryReadStringArray(Json, Position, Values) then
        exit;
      FoundCandidates := True;
    end else if not TrySkipJsonValue(Json, Position) then
      exit;
    SkipJsonWhitespace(Json, Position);
    if Position > Length(Json) then
      exit;
    if Json[Position] = ',' then begin
      Position := Position + 1;
      SkipJsonWhitespace(Json, Position);
      if (Position > Length(Json)) or (Json[Position] = '}') then
        exit;
    end else if Json[Position] <> '}' then
      exit;
  end;
  SkipJsonWhitespace(Json, Position);
  Result := Done and FoundCandidates and (Position > Length(Json));
end;

function IsAbsoluteWindowsPath(const Path: string): Boolean;
begin
  Result := ((Length(Path) >= 3) and (Path[2] = ':') and (Path[3] = '\')) or
    ((Length(Path) >= 5) and (Copy(Path, 1, 2) = '\\'));
end;

function NormalizePathForComparison(Path: string): string;
begin
  StringChangeEx(Path, '/', '\', True);
  while (Length(Path) > 3) and (Path[Length(Path)] = '\') do
    Delete(Path, Length(Path), 1);
  Result := Path;
end;

function StorageRootCandidatesAreSafe(const DataDir: string; const ConfigPath: string;
  var MatchesStorageRoot: Boolean): Boolean;
var
  Attributes, LastError: LongWord;
  RawConfig: AnsiString;
  Json: string;
  Candidates: TArrayOfString;
  I: Integer;
begin
  Result := False;
  MatchesStorageRoot := False;
  Attributes := GetFileAttributesW(ConfigPath);
  if Attributes = INVALID_FILE_ATTRIBUTES then begin
    LastError := GetLastError();
    if (LastError = ERROR_FILE_NOT_FOUND) or (LastError = ERROR_PATH_NOT_FOUND) then
      Result := True;
    exit;
  end;
  if (Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
    exit;
  if not LoadStringFromFile(ConfigPath, RawConfig) then
    exit;
  Json := UTF8Decode(RawConfig);
  if not TryReadStorageCandidates(Json, Candidates) then
    exit;
  for I := 0 to GetArrayLength(Candidates) - 1 do begin
    if not IsAbsoluteWindowsPath(Candidates[I]) then
      exit;
    if CompareText(NormalizePathForComparison(Candidates[I]),
       NormalizePathForComparison(DataDir)) = 0 then
      MatchesStorageRoot := True;
  end;
  Result := True;
end;

function PathAttributesAreSafe(const Path: string; RequireDirectory: Boolean): Boolean;
var
  Attributes, LastError: LongWord;
begin
  Attributes := GetFileAttributesW(Path);
  if Attributes = INVALID_FILE_ATTRIBUTES then begin
    LastError := GetLastError();
    Result := (LastError = ERROR_FILE_NOT_FOUND) or (LastError = ERROR_PATH_NOT_FOUND);
    exit;
  end;
  Result := ((Attributes and FILE_ATTRIBUTE_REPARSE_POINT) = 0) and
    (not RequireDirectory or ((Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0));
end;

function OptionalPathIsMissingOrSafe(const Path: string): Boolean;
var
  Attributes, LastError: LongWord;
begin
  Attributes := GetFileAttributesW(Path);
  if Attributes = INVALID_FILE_ATTRIBUTES then begin
    LastError := GetLastError();
    Result := (LastError = ERROR_FILE_NOT_FOUND) or (LastError = ERROR_PATH_NOT_FOUND);
    exit;
  end;
  Result := (Attributes and FILE_ATTRIBUTE_REPARSE_POINT) = 0;
end;

function OptionalFileIsMissingOrSafe(const Path: string): Boolean;
var
  Attributes, LastError: LongWord;
begin
  Attributes := GetFileAttributesW(Path);
  if Attributes = INVALID_FILE_ATTRIBUTES then begin
    LastError := GetLastError();
    Result := (LastError = ERROR_FILE_NOT_FOUND) or (LastError = ERROR_PATH_NOT_FOUND);
    exit;
  end;
  Result := ((Attributes and FILE_ATTRIBUTE_REPARSE_POINT) = 0) and
    ((Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0);
end;

function UserDataIsSafe(const DataDir: string; var Reason: string): Boolean;
var
  LogsDir, ConfigPath, MarkerPath, LogPath: string;
  MatchesStorageRoot: Boolean;
  I: Integer;
  Attributes, LastError: LongWord;
begin
  Result := False;
  Reason := '';
  LogsDir := AddBackslash(DataDir) + 'logs';
  ConfigPath := AddBackslash(DataDir) + 'config.json';
  if not StorageRootCandidatesAreSafe(DataDir, ConfigPath, MatchesStorageRoot) then begin
    Reason := 'config.json cannot be read or interpreted';
    exit;
  end;
  if MatchesStorageRoot then begin
    Reason := 'the application data directory is also a configured storage root';
    exit;
  end;
  for I := 0 to 2 do begin
    case I of
      0: MarkerPath := AddBackslash(DataDir) + '.maildock_root';
      1: MarkerPath := AddBackslash(DataDir) + 'metadata.db';
      2: MarkerPath := AddBackslash(DataDir) + 'manifests';
    end;
    if not OptionalPathIsMissingOrSafe(MarkerPath) then begin
      Reason := 'storage marker path cannot be inspected safely';
      exit;
    end;
    Attributes := GetFileAttributesW(MarkerPath);
    if Attributes <> INVALID_FILE_ATTRIBUTES then begin
      Reason := 'storage data marker exists in the settings directory';
      exit;
    end;
    LastError := GetLastError();
    if (LastError <> ERROR_FILE_NOT_FOUND) and (LastError <> ERROR_PATH_NOT_FOUND) then begin
      Reason := 'storage marker path cannot be inspected safely';
      exit;
    end;
  end;
  if not PathAttributesAreSafe(DataDir, True) then begin
    Reason := 'application data directory cannot be inspected safely';
    exit;
  end;
  if not PathAttributesAreSafe(LogsDir, True) then begin
    Reason := 'logs directory cannot be inspected safely';
    exit;
  end;
  if not OptionalFileIsMissingOrSafe(ConfigPath) then begin
    Reason := 'config.json is not a safe regular file';
    exit;
  end;
  for I := 0 to 5 do begin
    if I = 0 then
      LogPath := AddBackslash(LogsDir) + 'app.log'
    else
      LogPath := AddBackslash(LogsDir) + 'app.log.' + IntToStr(I);
    if not OptionalFileIsMissingOrSafe(LogPath) then begin
      Reason := 'an application log is not a safe regular file';
      exit;
    end;
  end;
  Result := True;
end;

procedure DeleteOwnedFile(const Path: string);
begin
  if FileExists(Path) and not DeleteFile(Path) then
    Log('Could not delete owned settings file: ' + Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, LogsDir, ConfigPath, LogPath, Reason: string;
  I: Integer;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  if IsAdminInstallMode then begin
    if not UninstallSilent then
      MsgBox(ExpandConstant('{cm:UninstallOtherUsers}'), mbInformation, MB_OK);
    exit;
  end;
  if UninstallSilent then
    exit;
  if MsgBox(ExpandConstant('{cm:UninstallSettingsPrompt}'), mbConfirmation,
     MB_YESNO or MB_DEFBUTTON2) <> IDYES then begin
    MsgBox(ExpandConstant('{cm:UninstallSettingsSkipped}'), mbInformation, MB_OK);
    exit;
  end;

  DataDir := ExpandConstant('{localappdata}\mail-dock');
  if not UserDataIsSafe(DataDir, Reason) then begin
    Log('Settings cleanup was skipped: ' + Reason);
    MsgBox(ExpandConstant('{cm:UninstallSettingsUnsafe}'), mbInformation, MB_OK);
    exit;
  end;
  LogsDir := AddBackslash(DataDir) + 'logs';
  ConfigPath := AddBackslash(DataDir) + 'config.json';
  DeleteOwnedFile(ConfigPath);
  for I := 0 to 5 do begin
    if I = 0 then
      LogPath := AddBackslash(LogsDir) + 'app.log'
    else
      LogPath := AddBackslash(LogsDir) + 'app.log.' + IntToStr(I);
    DeleteOwnedFile(LogPath);
  end;
  RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\mail-dock\mail-dock');
end;
