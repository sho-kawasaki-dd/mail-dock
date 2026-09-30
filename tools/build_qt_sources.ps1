[CmdletBinding()]
param(
    [string]$DistributionRoot = "dist\mail-dock\_internal",
    [string]$SourceOutputRoot = "build\sources\qt",
    [string]$LicenseOutputPath = "build\licenses\QT-SOURCE.md",
    [string]$ModuleMapPath = "packaging\qt\qt-module-map.json",
    [string]$SourceLockPath = "packaging\qt\qt-source.lock.json",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Resolve-RepositoryPath([string]$Path) {
    if ([IO.Path]::IsPathRooted($Path)) { return $Path }
    return Join-Path $repositoryRoot $Path
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Get-QtVersionFromUvLock([string]$Path) {
    $text = Get-Content -LiteralPath $Path -Raw
    $match = [regex]::Match($text, '(?ms)^\[\[package\]\]\r?\nname = "pyside6"\r?\nversion = "([^"]+)"')
    if (-not $match.Success) { throw "Could not find the locked pyside6 package version in uv.lock." }
    return $match.Groups[1].Value
}

function Get-CollectedQtModules([string]$Root, $Map) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "PyInstaller distribution directory does not exist: $Root"
    }
    $dlls = Get-ChildItem -LiteralPath $Root -Recurse -File -Filter "*.dll" | Where-Object {
        $_.Name -match '^(?i:Qt6|shiboken6|pyside6)' -or
        $_.FullName.Substring($Root.Length).Replace('\\', '/') -match '(?i)(^|/)PySide6/(plugins|qml)/'
    }
    if (-not $dlls) { throw "No Qt-related DLLs were found below $Root." }

    $modules = [System.Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $unmapped = [System.Collections.Generic.List[string]]::new()
    foreach ($dll in $dlls) {
        $relative = $dll.FullName.Substring($Root.Length).TrimStart('\', '/') -replace '\\', '/'
        $rule = $Map.mappings | Where-Object { $relative -match $_.pattern } | Select-Object -First 1
        if ($null -eq $rule) {
            $unmapped.Add($relative)
        } else {
            [void]$modules.Add($rule.module)
        }
    }
    if ($unmapped.Count -gt 0) {
        throw "Qt DLLs have no source-module mapping: $($unmapped -join ', ')"
    }
    if (-not $modules.Contains("pyside-setup")) {
        throw "The collected distribution does not contain a mapped PySide6/Shiboken DLL."
    }
    return @($modules)
}

function Assert-LockCoverage($Modules, $Lock) {
    $missing = @($Modules | Where-Object { $_ -ne "pyside-setup" -and -not $Lock.modules.Contains($_) })
    if ($missing.Count -gt 0) {
        throw "Qt source lock has no pinned archive for modules: $($missing -join ', ')"
    }
    foreach ($module in $Modules) {
        $source = if ($module -eq "pyside-setup") { $Lock.pyside_setup } else { $Lock.modules[$module] }
        if ($source.filename -notmatch '\.tar\.(xz|zst)$' -or $source.sha256 -notmatch '^[0-9A-Fa-f]{64}$') {
            throw "Invalid filename or SHA-256 in Qt source lock for $module."
        }
    }
}

function Get-LockedArchive($Source, [string]$CacheRoot) {
    $destination = Join-Path $CacheRoot $Source.filename
    if (Test-Path -LiteralPath $destination -PathType Leaf) {
        if ((Get-Sha256 $destination) -eq $Source.sha256.ToUpperInvariant()) { return $destination }
    }

    $temporary = "$destination.download"
    $downloadError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        Write-Host "Downloading $($Source.filename) (attempt $attempt/3)"
        try {
            Invoke-WebRequest -Uri $Source.url -OutFile $temporary
            $downloadError = $null
            break
        } catch {
            $downloadError = $_
        }
    }
    if ($null -ne $downloadError) {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        throw "Could not download locked Qt source $($Source.filename) after 3 attempts: $downloadError"
    }
    $actual = Get-Sha256 $temporary
    if ($actual -ne $Source.sha256.ToUpperInvariant()) {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        throw "Qt source SHA-256 mismatch for $($Source.filename): $actual"
    }
    Move-Item -LiteralPath $temporary -Destination $destination -Force
    return $destination
}

function New-CorrespondingSourceZip([string[]]$Archives, [string]$OutputPath) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $temporary = "$OutputPath.tmp"
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew)
    try {
        $zip = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($archive in $Archives | Sort-Object) {
                $entry = $zip.CreateEntry([IO.Path]::GetFileName($archive), [IO.Compression.CompressionLevel]::NoCompression)
                $inputStream = [IO.File]::OpenRead($archive)
                try {
                    $entryStream = $entry.Open()
                    try { $inputStream.CopyTo($entryStream) } finally { $entryStream.Dispose() }
                } finally { $inputStream.Dispose() }
            }
        } finally { $zip.Dispose() }
    } finally { $stream.Dispose() }
    Move-Item -LiteralPath $temporary -Destination $OutputPath -Force
}

$distributionRoot = Resolve-RepositoryPath $DistributionRoot
$sourceOutputRoot = Resolve-RepositoryPath $SourceOutputRoot
$licenseOutputPath = Resolve-RepositoryPath $LicenseOutputPath
$moduleMapPath = Resolve-RepositoryPath $ModuleMapPath
$sourceLockPath = Resolve-RepositoryPath $SourceLockPath
$uvLockPath = Join-Path $repositoryRoot "uv.lock"
$versionSourcePath = Join-Path $repositoryRoot "src\mail_dock\__init__.py"

try {
    $lock = Get-Content -LiteralPath $sourceLockPath -Raw | ConvertFrom-Json -AsHashtable
    $map = Get-Content -LiteralPath $moduleMapPath -Raw | ConvertFrom-Json
    $lockedPySideVersion = Get-QtVersionFromUvLock $uvLockPath
    if ($lockedPySideVersion -ne $lock.pyside_version -or $lockedPySideVersion -ne $lock.qt_version) {
        throw "PySide6/Qt version mismatch: uv.lock=$lockedPySideVersion, source lock PySide=$($lock.pyside_version), Qt=$($lock.qt_version)."
    }
    $versionMatch = [regex]::Match((Get-Content -LiteralPath $versionSourcePath -Raw), '__version__\s*=\s*["'']([^"'']+)')
    if (-not $versionMatch.Success) { throw "Could not read mail_dock.__version__." }
    $appVersion = $versionMatch.Groups[1].Value
    $modules = Get-CollectedQtModules $distributionRoot $map
    Assert-LockCoverage $modules $lock

    Write-Host "PySide6/Qt version: $lockedPySideVersion"
    Write-Host "Mapped Qt source modules: $($modules -join ', ')"
    if ($ValidateOnly) { exit 0 }

    New-Item -ItemType Directory -Force -Path $sourceOutputRoot | Out-Null
    $selectedSources = [System.Collections.Generic.List[object]]::new()
    foreach ($module in $modules) {
        $source = if ($module -eq "pyside-setup") { $lock.pyside_setup } else { $lock.modules[$module] }
        $path = Get-LockedArchive $source $sourceOutputRoot
        $selectedSources.Add([PSCustomObject]@{ module = $module; source = $source; path = $path })
    }

    $assetName = "mail-dock-$appVersion-qt-corresponding-source.zip"
    $assetPath = Join-Path $sourceOutputRoot $assetName
    New-CorrespondingSourceZip @($selectedSources | ForEach-Object { $_.path }) $assetPath
    $assetHash = Get-Sha256 $assetPath
    $assetLength = (Get-Item -LiteralPath $assetPath).Length

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("# Qt and PySide6 Corresponding Source")
    $lines.Add("")
    $lines.Add("Application version: ``$appVersion``  ")
    $lines.Add("PySide6 version: ``$($lock.pyside_version)``  ")
    $lines.Add("Qt version: ``$($lock.qt_version)``")
    $lines.Add("")
    $lines.Add("The release asset contains the unmodified, version-matched source archives listed below. The module set was derived from the Qt-related DLLs collected in the PyInstaller distribution; the PySide/shiboken bindings source is always included.")
    $lines.Add("")
    $lines.Add("| Component | Source archive | Source URL | SHA-256 |")
    $lines.Add("| --- | --- | --- | --- |")
    foreach ($item in $selectedSources | Sort-Object module) {
        $lines.Add("| ``$($item.module)`` | ``$($item.source.filename)`` | $($item.source.url) | ``$($item.source.sha256.ToUpperInvariant())`` |")
    }
    $lines.Add("")
    $lines.Add("Release asset: ``$assetName``  ")
    $lines.Add("Asset size: $assetLength bytes  ")
    $lines.Add("Asset SHA-256: ``$assetHash``")
    $lines.Add("")
    $lines.Add("The asset is an uncompressed zip containing the original source archives. Each archive remains independently verifiable by the SHA-256 listed above.")
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $licenseOutputPath) | Out-Null
    [IO.File]::WriteAllLines($licenseOutputPath, $lines, [Text.UTF8Encoding]::new($false))
    Write-Host "Created $assetPath"
    Write-Host "Wrote $licenseOutputPath"
} catch {
    Write-Error $_
    exit 1
}