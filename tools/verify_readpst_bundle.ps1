[CmdletBinding()]
param(
    [string]$VendorDir = "",
    [string]$SourceRoot = "",
    [string]$SourceBundle = "",
    [string]$LockPath = "",
    [string]$ZstdPath = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($VendorDir)) { $VendorDir = Join-Path $repositoryRoot "vendor\readpst" }
if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $SourceRoot = Join-Path $repositoryRoot "build\sources\msys2" }
if ([string]::IsNullOrWhiteSpace($SourceBundle)) { $SourceBundle = Join-Path $SourceRoot "mail-dock-readpst-corresponding-source.zip" }
if ([string]::IsNullOrWhiteSpace($LockPath)) { $LockPath = Join-Path $repositoryRoot "packaging\readpst\msys2-packages.lock.json" }
if ([string]::IsNullOrWhiteSpace($ZstdPath)) { $ZstdPath = "C:\msys64\usr\bin\zstd.exe" }
$lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
$metadataPath = Join-Path $VendorDir "readpst-artifacts.json"
$checksumPath = Join-Path $VendorDir "SHA256SUMS"
$metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
$required = @("readpst.exe", "lspst.exe", "COPYING", "readpst.exe.manifest", "readpst-artifacts.json", "SHA256SUMS")
foreach ($name in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $VendorDir $name) -PathType Leaf)) { throw "Missing required readpst/GPL artifact: $name" }
}
if ((Get-Content -LiteralPath (Join-Path $VendorDir "COPYING") -Raw) -notmatch "GNU GENERAL PUBLIC LICENSE") { throw "vendor/readpst/COPYING is not a GPL license text." }
if ($metadata.package.licenses -notmatch "GPL-2\.0") { throw "The libpst package metadata does not identify GPL-2.0-or-later." }
if ($metadata.source.commit -notmatch "^[0-9a-f]{40}$") { throw "The libpst source commit is not pinned." }

foreach ($artifact in $metadata.artifacts) {
    $path = Join-Path $VendorDir ($artifact.path.Replace("/", "\"))
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Manifest-listed artifact is missing: $($artifact.path)" }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $artifact.sha256) { throw "Artifact SHA-256 mismatch: $($artifact.path)" }
}
$listedChecksums = @{}
foreach ($line in Get-Content -LiteralPath $checksumPath) {
    if ($line -notmatch "^([0-9A-Fa-f]{64})\s{2}(.+)$") { throw "Malformed SHA256SUMS entry: $line" }
    $relative = $Matches[2]
    $path = Join-Path $VendorDir $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "SHA256SUMS references a missing file: $relative" }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $Matches[1]) { throw "SHA256SUMS hash mismatch: $relative" }
    $listedChecksums[$relative.Replace("\", "/")] = $true
}
foreach ($artifact in $metadata.artifacts) {
    if (-not $listedChecksums.ContainsKey($artifact.path.Replace("\", "/"))) { throw "SHA256SUMS does not cover manifest-listed artifact: $($artifact.path)" }
}
if (-not $listedChecksums.ContainsKey("readpst-artifacts.json")) { throw "SHA256SUMS does not cover readpst-artifacts.json." }
if ($metadata.readpst_manifest.readpst_sha256_before -notmatch "^[0-9A-F]{64}$" -or $metadata.readpst_manifest.readpst_sha256_after -notmatch "^[0-9A-F]{64}$") { throw "The readpst pre/post manifest-patch hashes are missing." }

$tarPath = Join-Path $env:SystemRoot "System32\tar.exe"
foreach ($source in $lock.sources) {
    if (-not $source.contains_upstream) { throw "Source package $($source.pkgbase) needs a separately pinned upstream source archive." }
    $sourcePath = Join-Path $SourceRoot $source.filename
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { throw "Missing source package: $($source.filename)" }
    if ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -ine $source.sha256) { throw "Source package hash mismatch: $($source.filename)" }
    $tarFile = "$sourcePath.expanded"
    & $ZstdPath -d -q -f $sourcePath -o $tarFile
    if ($LASTEXITCODE -ne 0) { throw "Could not decompress source package: $($source.filename)" }
    try {
        $entries = @(& $tarPath -tf $tarFile)
        if ($LASTEXITCODE -ne 0) { throw "Could not list source package: $($source.filename)" }
        if (-not ($entries -contains "$($source.pkgbase)/PKGBUILD")) { throw "Source package has no PKGBUILD: $($source.filename)" }
        $upstreamPresent = @($entries | Where-Object { $_ -match '\.(tar\.(gz|xz|bz2|lz)|zip)$' -or $_ -match '/objects/pack/.*\.pack$' -or $_ -match '/[^/]+\.(c|h|cc|cpp)$' }).Count -gt 0
        if (-not $upstreamPresent) { throw "Source package does not visibly contain upstream source: $($source.filename)" }
    } finally { Remove-Item -LiteralPath $tarFile -Force -ErrorAction SilentlyContinue }
}
$libpstSourcePath = Join-Path $VendorDir $lock.libpst_source.filename
if (-not (Test-Path -LiteralPath $libpstSourcePath -PathType Leaf) -or (Get-FileHash $libpstSourcePath -Algorithm SHA256).Hash -ine $lock.libpst_source.sha256) { throw "Pinned libpst upstream source is missing or has the wrong hash." }

if (-not (Test-Path -LiteralPath $SourceBundle -PathType Leaf)) { throw "Corresponding source bundle is missing: $SourceBundle" }
$bundleStage = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $bundleStage | Out-Null
try {
    Expand-Archive -LiteralPath $SourceBundle -DestinationPath $bundleStage -Force
    foreach ($source in $lock.sources) {
        $bundledSource = Join-Path $bundleStage $source.filename
        if (-not (Test-Path -LiteralPath $bundledSource -PathType Leaf) -or (Get-FileHash $bundledSource -Algorithm SHA256).Hash -ine $source.sha256) { throw "Corresponding source bundle lacks verified source: $($source.filename)" }
    }
    $bundledLibpstSource = Join-Path $bundleStage $lock.libpst_source.filename
    if (-not (Test-Path -LiteralPath $bundledLibpstSource -PathType Leaf) -or (Get-FileHash $bundledLibpstSource -Algorithm SHA256).Hash -ine $lock.libpst_source.sha256) { throw "Corresponding source bundle lacks the pinned libpst upstream archive." }
    $bundledManifest = Join-Path $bundleStage "readpst.exe.manifest"
    if (-not (Test-Path -LiteralPath $bundledManifest -PathType Leaf) -or (Get-FileHash $bundledManifest -Algorithm SHA256).Hash -ine $metadata.readpst_manifest.sha256) { throw "Corresponding source bundle lacks the exact readpst manifest used for the binary." }
    if (-not (Test-Path (Join-Path $bundleStage "SOURCE-BUILD.md")) -or -not (Test-Path (Join-Path $bundleStage "readpst-source-inventory.json"))) { throw "Corresponding source bundle is missing build instructions or binary/source mapping." }
    $inventory = Get-Content (Join-Path $bundleStage "readpst-source-inventory.json") -Raw | ConvertFrom-Json
    foreach ($source in $lock.sources) {
        $mapping = $inventory.sources | Where-Object pkgbase -eq $source.pkgbase
        if ($null -eq $mapping -or $mapping.filename -ne $source.filename -or $mapping.sha256 -ine $source.sha256) { throw "Corresponding source inventory is incomplete for $($source.pkgbase)." }
    }
    foreach ($package in $lock.packages) {
        $mapping = $inventory.binaries | Where-Object name -eq $package.name
        $expectedFiles = @($package.extracted_files | ForEach-Object destination | Sort-Object)
        $mappedFiles = @($mapping.extracted_files | Sort-Object)
        if ($null -eq $mapping -or $mapping.version -ne $package.version -or $mapping.filename -ne $package.binary.filename -or $mapping.sha256 -ine $package.binary.sha256 -or $mapping.source_ref -ne $package.source_ref -or (Compare-Object $expectedFiles $mappedFiles)) { throw "Corresponding source mapping is missing or differs for $($package.name)." }
    }
} finally { Remove-Item -LiteralPath $bundleStage -Recurse -Force -ErrorAction SilentlyContinue }

Write-Host "readpst GPL materials, package/source hashes, PKGBUILDs, and corresponding-source bundle are valid."