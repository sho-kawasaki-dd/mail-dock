[CmdletBinding()]
param(
    [string]$Msys2Root = "C:\msys64",
    [string]$SourceCache = "",
    [string]$LockPath = "",
    [string]$SourceCommit = "",
    [switch]$UploadMirror,
    [string]$MirrorRepository = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($SourceCache)) { $SourceCache = Join-Path $repositoryRoot "build\sources\msys2" }
if ([string]::IsNullOrWhiteSpace($LockPath)) { $LockPath = Join-Path $repositoryRoot "packaging\readpst\msys2-packages.lock.json" }
$pacman = Join-Path $Msys2Root "usr\bin\pacman.exe"
$zstd = Join-Path $Msys2Root "usr\bin\zstd.exe"
$tar = Join-Path $env:SystemRoot "System32\tar.exe"
$ldd = Join-Path $Msys2Root "usr\bin\ldd.exe"
$ucrtBin = Join-Path $Msys2Root "ucrt64\bin"
foreach ($tool in @($pacman, $zstd, $tar, $ldd)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Required MSYS2/build tool not found: $tool" }
}
$oldLock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force -Path $SourceCache | Out-Null

function Invoke-Checked([string]$Path, [string[]]$Arguments) {
    $result = & $Path @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Path $($Arguments -join ' ')`n$($result -join "`n")" }
    return $result
}

function Get-Field([string]$Text, [string]$Name) {
    $match = [regex]::Match($Text, "(?m)^$([regex]::Escape($Name))\s*=\s*(.+)$")
    if (-not $match.Success) { throw "Package metadata is missing '$Name'." }
    return $match.Groups[1].Value.Trim()
}

function Get-Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant() }

function Get-SourceContents([string]$Archive) {
    $tarFile = "$Archive.expanded"
    Invoke-Checked $zstd @("-d", "-q", "-f", $Archive, "-o", $tarFile) | Out-Null
    try { return @(Invoke-Checked $tar @("-tf", $tarFile)) } finally { Remove-Item $tarFile -Force -ErrorAction SilentlyContinue }
}

function Get-RepositorySlug {
    if (-not [string]::IsNullOrWhiteSpace($MirrorRepository)) { return $MirrorRepository.TrimEnd("/").Replace(".git", "") }
    $remote = (& git -C $repositoryRoot remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0 -or $remote -notmatch "github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$") { throw "Could not infer GitHub owner/repository." }
    return $Matches[1]
}

$runtimeDlls = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($executable in @("readpst.exe", "lspst.exe")) {
    $output = Invoke-Checked $ldd @((Join-Path $ucrtBin $executable))
    foreach ($line in $output) {
        if ($line -match "^\s*(\S+\.dll)\s+=>\s+(\S+)") {
            if ($Matches[2] -eq "not" -or $Matches[2] -match "not found") { throw "Unresolved runtime dependency in ldd output: $line" }
            $dllName = [System.IO.Path]::GetFileName($Matches[1])
            if ($Matches[2] -match "(?i)(/ucrt64/bin/|\\ucrt64\\bin\\)") {
                [void]$runtimeDlls.Add($dllName)
            }
        }
    }
}
$runtimeDlls.Add("libpst-4.dll") | Out-Null
$packageNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$packageNames.Add("mingw-w64-ucrt-x86_64-libpst") | Out-Null
foreach ($dllName in $runtimeDlls) {
    $dllPath = Join-Path $ucrtBin $dllName
    if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) { throw "ldd dependency is missing from UCRT64: $dllName" }
    $msysPath = "/ucrt64/bin/$dllName"
    $owner = Invoke-Checked $pacman @("-Qo", $msysPath)
    if (($owner -join " ") -match "owned by\s+(\S+)") { [void]$packageNames.Add($Matches[1]) }
}

$pythonLocator = Get-Content -LiteralPath (Join-Path $repositoryRoot "src\mail_dock\infrastructure\importers\readpst_locator.py") -Raw
$locatorDlls = [regex]::Matches($pythonLocator, '"([A-Za-z0-9_.+-]+\.dll)"') | ForEach-Object { $_.Groups[1].Value }
$missingInLocator = @($runtimeDlls | Where-Object { $_ -notin $locatorDlls })
$extraInLocator = @($locatorDlls | Where-Object { $_ -notin $runtimeDlls })
if ($missingInLocator.Count -or $extraInLocator.Count) {
    Write-Warning "ldd DLL set differs from readpst_locator._WINDOWS_READPST_DLLS. Missing from locator: $($missingInLocator -join ', '); no longer needed: $($extraInLocator -join ', '). Update the locator and its consistency test."
}

$packageRecords = @()
$sourceRecords = @{}
foreach ($name in ($packageNames | Sort-Object)) {
    $info = (Invoke-Checked $pacman @("-Qi", $name)) -join "`n"
    $version = [regex]::Match($info, "(?m)^Version\s*:\s*(.+)$").Groups[1].Value.Trim()
    $projectUrl = [regex]::Match($info, "(?m)^URL\s*:\s*(.+)$").Groups[1].Value.Trim()
    $license = [regex]::Match($info, "(?m)^Licenses\s*:\s*(.+)$").Groups[1].Value.Trim()
    if (-not $version -or -not $license) { throw "Could not read version/license for $name." }
    $binaryPattern = Join-Path $Msys2Root "var\cache\pacman\pkg\$name-$version-*.pkg.tar.zst"
    $binary = Get-ChildItem -Path $binaryPattern -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $binary) {
        Invoke-Checked $pacman @("-Sw", "--noconfirm", $name) | Out-Null
        $binary = Get-ChildItem -Path $binaryPattern -File -ErrorAction Stop | Select-Object -First 1
    }
    $binaryTar = "$($binary.FullName).expanded"
    Invoke-Checked $zstd @("-d", "-q", "-f", $binary.FullName, "-o", $binaryTar) | Out-Null
    try {
        $pkgInfo = (Invoke-Checked $tar @("-xOf", $binaryTar, ".PKGINFO")) -join "`n"
        $pkgbase = [regex]::Match($pkgInfo, "(?m)^pkgbase\s*=\s*(.+)$").Groups[1].Value.Trim()
        $archiveEntries = @(Invoke-Checked $tar @("-tf", $binaryTar))
    } finally { Remove-Item $binaryTar -Force -ErrorAction SilentlyContinue }
    if (-not $pkgbase) { throw "The package $name has no pkgbase in .PKGINFO." }
    $selected = @()
    foreach ($entry in $archiveEntries) {
        $leaf = [System.IO.Path]::GetFileName($entry)
        $wanted = $runtimeDlls.Contains($leaf) -or $leaf -in @("readpst.exe", "lspst.exe") -or ($name -match "libpst$" -and $entry -match "/share/(doc|licenses)/libpst/COPYING$")
        if ($wanted) { $selected += [ordered]@{ archive_path = $entry; destination = $leaf } }
    }
    $packageRecords += [ordered]@{
        name = $name; version = $version
        binary = [ordered]@{ filename = $binary.Name; url = "https://repo.msys2.org/mingw/ucrt64/$($binary.Name)"; sha256 = Get-Hash $binary.FullName }
        source_ref = $pkgbase; license = $license; project_url = $projectUrl; extracted_files = @($selected)
    }
    if (-not $sourceRecords.ContainsKey($pkgbase)) {
        $sourceFilename = "$pkgbase-$version.src.tar.zst"
        $sourceUrl = "https://repo.msys2.org/mingw/sources/$sourceFilename"
        $sourcePath = Join-Path $SourceCache $sourceFilename
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            Invoke-WebRequest -Uri $sourceUrl -OutFile $sourcePath -UseBasicParsing
        }
        $sourceNames = Get-SourceContents $sourcePath
        $upstreamPresent = @($sourceNames | Where-Object { $_ -match '\.(tar\.(gz|xz|bz2|lz)|zip)$' -or $_ -match '/objects/pack/.*\.pack$' -or $_ -match '/[^/]+\.(c|h|cc|cpp)$' }).Count -gt 0
        if (-not $upstreamPresent) { throw "Source package $sourceFilename has no detectable upstream source; pin an additional upstream archive before refreshing the lock." }
        $sourceRecords[$pkgbase] = [ordered]@{ pkgbase = $pkgbase; version = $version; filename = $sourceFilename; url = $sourceUrl; sha256 = Get-Hash $sourcePath; contains_upstream = $upstreamPresent }
    }
}

$oldLibpst = $oldLock.libpst_source
if ([string]::IsNullOrWhiteSpace($SourceCommit)) { $SourceCommit = $oldLibpst.commit }
if ($SourceCommit -notmatch "^[0-9a-f]{40}$") { throw "-SourceCommit must be a full 40-character upstream commit." }
$newLock = [ordered]@{
    schema_version = 1
    mirror_release_tag = "readpst-msys2-mirror"
    binary_base_url = "https://repo.msys2.org/mingw/ucrt64"
    source_base_url = "https://repo.msys2.org/mingw/sources"
    packages = @($packageRecords)
    sources = @($sourceRecords.Values | Sort-Object pkgbase)
    libpst_source = $oldLibpst
}
if ($SourceCommit -ne $oldLibpst.commit) {
    $sourceFilename = "libpst-$SourceCommit.tar.gz"
    $sourceUrl = "https://github.com/pst-format/libpst/archive/$SourceCommit.tar.gz"
    $sourcePath = Join-Path $SourceCache $sourceFilename
    Invoke-WebRequest -Uri $sourceUrl -OutFile $sourcePath -UseBasicParsing
    $newLock.libpst_source = [ordered]@{ commit = $SourceCommit; filename = $sourceFilename; url = $sourceUrl; sha256 = Get-Hash $sourcePath }
}
$newLock | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $LockPath -Encoding utf8NoBOM
Write-Host "Updated lock file: $LockPath"

if ($UploadMirror) {
    $gh = Get-Command gh.exe -ErrorAction SilentlyContinue
    if ($null -eq $gh) { throw "gh.exe is required for mirror upload." }
    & $gh.Source auth status
    if ($LASTEXITCODE -ne 0) { throw "Authenticate with gh auth login before uploading mirror assets." }
    if ([string]::IsNullOrWhiteSpace($MirrorRepository)) {
        $remote = (& git -C $repositoryRoot remote get-url origin 2>$null)
        if ($remote -notmatch "github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$") { throw "Could not infer GitHub owner/repository." }
        $MirrorRepository = $Matches[1]
    }
    & $gh.Source release view readpst-msys2-mirror -R $MirrorRepository *> $null
    if ($LASTEXITCODE -ne 0) {
        & $gh.Source release create readpst-msys2-mirror -R $MirrorRepository --title "MSYS2 readpst package mirror" --notes "Immutable package mirrors used by mail-dock packaging/readpst/msys2-packages.lock.json."
        if ($LASTEXITCODE -ne 0) { throw "Could not create readpst-msys2-mirror release." }
    }
    $assets = @($packageRecords | ForEach-Object { Join-Path (Join-Path $Msys2Root "var\cache\pacman\pkg") $_.binary.filename })
    $assets += @($sourceRecords.Values | ForEach-Object { Join-Path $SourceCache $_.filename })
    & $gh.Source release upload readpst-msys2-mirror @assets -R $MirrorRepository --clobber
    if ($LASTEXITCODE -ne 0) { throw "Could not upload all pinned MSYS2 package mirror assets." }
}