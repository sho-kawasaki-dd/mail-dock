[CmdletBinding()]
param(
    [string]$Msys2Root = "C:\msys64",
    [string]$OutputRoot = "",
    [string]$SourceRoot = "",
    [string]$LockPath = "",
    [string]$ZstdPath = "",
    [string]$MirrorRepository = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $repositoryRoot "vendor\readpst" }
if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = Join-Path $repositoryRoot "build\sources\msys2" }
if ([string]::IsNullOrWhiteSpace($LockPath)) { $LockPath = Join-Path $repositoryRoot "packaging\readpst\msys2-packages.lock.json" }
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$SourceRoot = [System.IO.Path]::GetFullPath($SourceRoot)
New-Item -ItemType Directory -Force -Path $OutputRoot, $SourceRoot | Out-Null
$lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json

if ([string]::IsNullOrWhiteSpace($ZstdPath)) { $ZstdPath = Join-Path $Msys2Root "usr\bin\zstd.exe" }
if (-not (Test-Path -LiteralPath $ZstdPath -PathType Leaf)) {
    $zstdCommand = Get-Command zstd.exe -ErrorAction SilentlyContinue
    if ($null -eq $zstdCommand) { throw "zstd.exe was not found. Set -ZstdPath or install MSYS2." }
    $ZstdPath = $zstdCommand.Source
}
$tarPath = Join-Path $env:SystemRoot "System32\tar.exe"
if (-not (Test-Path -LiteralPath $tarPath -PathType Leaf)) { throw "tar.exe was not found: $tarPath" }

function Get-RepositorySlug {
    if (-not [string]::IsNullOrWhiteSpace($MirrorRepository)) { return $MirrorRepository.TrimEnd("/").Replace(".git", "") }
    $remote = (& git -C $repositoryRoot remote get-url origin 2>$null)
    if ($LASTEXITCODE -ne 0 -or $remote -notmatch "github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$") {
        throw "Could not infer a GitHub owner/repository for the readpst mirror fallback."
    }
    return $Matches[1]
}

function Get-Sha256([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant() }

function Get-VerifiedFile($Record, [string]$Destination, [bool]$UseMirror) {
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and (Get-Sha256 $Destination) -eq $Record.sha256) { return $Destination }
    Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    $temporary = "$Destination.download"
    try {
        Invoke-WebRequest -Uri $Record.url -OutFile $temporary -UseBasicParsing
        if ((Get-Sha256 $temporary) -ne $Record.sha256) { throw "SHA-256 mismatch from $($Record.url)" }
    } catch {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        if (-not $UseMirror) { throw }
        $slug = Get-RepositorySlug
        $mirrorUrl = "https://github.com/$slug/releases/download/$($lock.mirror_release_tag)/$($Record.filename)"
        Invoke-WebRequest -Uri $mirrorUrl -OutFile $temporary -UseBasicParsing
        if ((Get-Sha256 $temporary) -ne $Record.sha256) { throw "SHA-256 mismatch from mirror asset $($Record.filename)" }
    }
    Move-Item -LiteralPath $temporary -Destination $Destination -Force
    return $Destination
}

function Invoke-NativeChecked([string]$FilePath, [string[]]$Arguments) {
    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')`n$($output -join "`n")" }
}

$packageFiles = @{}
foreach ($package in $lock.packages) {
    $binaryPath = Join-Path $SourceRoot $package.binary.filename
    Get-VerifiedFile $package.binary $binaryPath $true | Out-Null
    $packageFiles[$package.name] = $binaryPath
}
foreach ($source in $lock.sources) {
    Get-VerifiedFile $source (Join-Path $SourceRoot $source.filename) $true | Out-Null
}

$stagingRoot = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stagingRoot | Out-Null
try {
    foreach ($package in $lock.packages) {
        $tarFile = Join-Path $stagingRoot "$($package.name).tar"
        $extractRoot = Join-Path $stagingRoot $package.name
        New-Item -ItemType Directory -Path $extractRoot | Out-Null
        Invoke-NativeChecked $ZstdPath @("-d", "-q", "-f", $packageFiles[$package.name], "-o", $tarFile)
        $archivePaths = @($package.extracted_files | ForEach-Object { $_.archive_path })
        Invoke-NativeChecked $tarPath (@("-xf", $tarFile, "-C", $extractRoot) + $archivePaths)
        foreach ($file in $package.extracted_files) {
            $sourceFile = Join-Path $extractRoot ($file.archive_path.Replace("/", "\"))
            if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { throw "Package '$($package.name)' does not contain '$($file.archive_path)'" }
            Copy-Item -LiteralPath $sourceFile -Destination (Join-Path $OutputRoot $file.destination) -Force
        }
    }
} finally {
    Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$libpstSource = $lock.libpst_source
$libpstSourcePath = Join-Path $OutputRoot $libpstSource.filename
Get-VerifiedFile $libpstSource $libpstSourcePath $false | Out-Null
$manifestPath = Join-Path $OutputRoot "readpst.exe.manifest"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "Authored readpst manifest is missing: $manifestPath" }
$readpstPath = Join-Path $OutputRoot "readpst.exe"
$beforeManifestHash = Get-Sha256 $readpstPath
$mtCandidates = @()
if (${env:ProgramFiles(x86)}) {
    $mtCandidates = @(Get-ChildItem -Path (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin\*\x64\mt.exe") -File -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending)
}
if ($mtCandidates.Count -eq 0) { throw "mt.exe was not found under the Windows 10 SDK. Install the Windows SDK." }
& $mtCandidates[0].FullName -manifest $manifestPath "-outputresource:$readpstPath;#1"
if ($LASTEXITCODE -ne 0) { throw "mt.exe failed to apply the readpst manifest." }
$afterManifestHash = Get-Sha256 $readpstPath

$artifactPaths = @($lock.packages | ForEach-Object { $_.extracted_files } | ForEach-Object { Join-Path $OutputRoot $_.destination })
$artifactPaths += @($libpstSourcePath, $manifestPath)
$artifacts = foreach ($path in $artifactPaths | Sort-Object -Unique) {
    [ordered]@{ path = [System.IO.Path]::GetRelativePath($OutputRoot, $path).Replace("\", "/"); sha256 = Get-Sha256 $path; size_bytes = (Get-Item -LiteralPath $path).Length }
}
$libpstPackage = $lock.packages | Where-Object name -eq "mingw-w64-ucrt-x86_64-libpst"
$metadata = [ordered]@{
    schema_version = 2
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    package = [ordered]@{ name = $libpstPackage.name; version = $libpstPackage.version; url = $libpstPackage.project_url; licenses = $libpstPackage.license }
    dependencies = @($lock.packages | ForEach-Object { [ordered]@{ name = $_.name; version = $_.version; url = $_.project_url; licenses = $_.license } })
    source = [ordered]@{ repository = "https://github.com/pst-format/libpst"; commit = $libpstSource.commit; url = $libpstSource.url; archive = $libpstSource.filename; sha256 = $libpstSource.sha256 }
    source_packages = @($lock.sources | ForEach-Object { [ordered]@{ pkgbase = $_.pkgbase; filename = $_.filename; sha256 = $_.sha256; contains_upstream = $_.contains_upstream } })
    readpst_manifest = [ordered]@{ path = "readpst.exe.manifest"; sha256 = Get-Sha256 $manifestPath; readpst_sha256_before = $beforeManifestHash; readpst_sha256_after = $afterManifestHash }
    artifacts = @($artifacts)
}
$metadataPath = Join-Path $OutputRoot "readpst-artifacts.json"
$metadata | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $metadataPath -Encoding utf8NoBOM
$checksumLines = foreach ($artifact in $artifacts) { "{0}  {1}" -f $artifact.sha256, $artifact.path }
$checksumLines += "{0}  readpst-artifacts.json" -f (Get-Sha256 $metadataPath)
$checksumLines | Set-Content -LiteralPath (Join-Path $OutputRoot "SHA256SUMS") -Encoding ascii

$bundleStage = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $bundleStage | Out-Null
try {
    foreach ($source in $lock.sources) { Copy-Item -LiteralPath (Join-Path $SourceRoot $source.filename) -Destination $bundleStage }
    Copy-Item -LiteralPath $libpstSourcePath -Destination $bundleStage
    Copy-Item -LiteralPath $manifestPath -Destination $bundleStage
    $inventory = [ordered]@{
        binaries = @($lock.packages | ForEach-Object { [ordered]@{ name = $_.name; version = $_.version; filename = $_.binary.filename; sha256 = $_.binary.sha256; source_ref = $_.source_ref; extracted_files = @($_.extracted_files | ForEach-Object destination) } })
        sources = @($lock.sources | ForEach-Object { [ordered]@{ pkgbase = $_.pkgbase; filename = $_.filename; sha256 = $_.sha256; contains_upstream = $_.contains_upstream } })
        additional_upstream = $libpstSource
        readpst_manifest = [ordered]@{ filename = "readpst.exe.manifest"; sha256 = Get-Sha256 $manifestPath }
    }
    $inventory | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $bundleStage "readpst-source-inventory.json") -Encoding utf8NoBOM
    @"
# MSYS2 readpst corresponding source

This archive contains each pinned MSYS2 source package, including PKGBUILD files and package patches, plus the exact libpst upstream source commit. Binary-to-source mappings and SHA-256 values are in readpst-source-inventory.json.

Reproduce packages in a UCRT64 MSYS2 environment with makepkg-mingw using the included PKGBUILD files. Verify source hashes against packaging/readpst/msys2-packages.lock.json. The shipped readpst.exe additionally has the tracked readpst.exe.manifest applied to its RT_MANIFEST resource with the Windows SDK mt.exe; no application source is modified by that step.
"@ | Set-Content -LiteralPath (Join-Path $bundleStage "SOURCE-BUILD.md") -Encoding utf8NoBOM
    $sourceBundle = Join-Path $SourceRoot "mail-dock-readpst-corresponding-source.zip"
    Remove-Item -LiteralPath $sourceBundle -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $bundleStage "*") -DestinationPath $sourceBundle -CompressionLevel Optimal
} finally {
    Remove-Item -LiteralPath $bundleStage -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "readpst bundle verified and corresponding sources assembled: $OutputRoot"
Write-Host "MSYS2 package cache: $SourceRoot"