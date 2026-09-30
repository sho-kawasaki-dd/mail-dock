[CmdletBinding()]
param(
    [string]$DistributionRoot = "",
    [string]$VendorDir = "",
    [string]$LicenseRoot = "",
    [string]$ZstdPath = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($DistributionRoot)) { $DistributionRoot = Join-Path $repositoryRoot "dist\mail-dock\_internal" }
if ([string]::IsNullOrWhiteSpace($VendorDir)) { $VendorDir = Join-Path $repositoryRoot "vendor\readpst" }
if ([string]::IsNullOrWhiteSpace($LicenseRoot)) { $LicenseRoot = Join-Path $DistributionRoot "licenses" }
if ([string]::IsNullOrWhiteSpace($ZstdPath)) { $ZstdPath = "C:\msys64\usr\bin\zstd.exe" }

function Get-MtExe {
    $programFilesX86 = ${env:ProgramFiles(x86)}
    if ([string]::IsNullOrWhiteSpace($programFilesX86)) { throw "Windows SDK mt.exe is unavailable." }
    $candidates = @(Get-ChildItem -Path (Join-Path $programFilesX86 "Windows Kits\10\bin\*\x64\mt.exe") -File -ErrorAction SilentlyContinue |
        Sort-Object { [version]$_.Directory.Parent.Name } -Descending)
    if ($candidates.Count -eq 0) { throw "Windows SDK mt.exe was not found." }
    return $candidates[0].FullName
}

function Assert-FileHash([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required release file is missing: $Path" }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($actual -ine $Expected) { throw "SHA-256 mismatch: $Path" }
}

$executable = Join-Path (Split-Path $DistributionRoot -Parent) "mail-dock.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw "Frozen application executable is missing: $executable" }

$manifestOutput = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N") + ".manifest")
try {
    & (Get-MtExe) "-inputresource:$executable;#1" "-out:$manifestOutput"
    if ($LASTEXITCODE -ne 0) { throw "Could not extract the mail-dock executable manifest." }
    [xml]$manifest = Get-Content -LiteralPath $manifestOutput -Raw
    $longPathNode = $manifest.SelectSingleNode("//*[local-name()='longPathAware']")
    if ($null -eq $longPathNode -or $longPathNode.InnerText.Trim().ToLowerInvariant() -ne "true") {
        throw "The mail-dock executable manifest does not enable longPathAware."
    }
}
finally {
    Remove-Item -LiteralPath $manifestOutput -Force -ErrorAction SilentlyContinue
}

$vendorMetadata = Get-Content -LiteralPath (Join-Path $VendorDir "readpst-artifacts.json") -Raw | ConvertFrom-Json
$checksumPath = Join-Path $VendorDir "SHA256SUMS"
$checksums = @{}
foreach ($line in Get-Content -LiteralPath $checksumPath) {
    if ($line -notmatch "^([0-9A-Fa-f]{64})\s{2}(.+)$") { throw "Malformed readpst SHA256SUMS entry." }
    $checksums[$Matches[2].Replace("\", "/")] = $Matches[1]
}
$bundledVendor = Join-Path $DistributionRoot "vendor\readpst"
$sourceArchiveName = [string]$vendorMetadata.source.archive
foreach ($artifact in $vendorMetadata.artifacts) {
    $relative = ([string]$artifact.path).Replace("/", "\")
    if ($relative -eq $sourceArchiveName -or $relative -like "libpst-*.tar.gz") { continue }
    $sourceHash = $checksums[[string]$artifact.path]
    if ([string]::IsNullOrWhiteSpace($sourceHash)) { throw "SHA256SUMS does not list $($artifact.path)." }
    Assert-FileHash (Join-Path $bundledVendor $relative) $sourceHash
}
foreach ($required in @("COPYING", "readpst-artifacts.json", "SHA256SUMS")) {
    if (-not (Test-Path -LiteralPath (Join-Path $bundledVendor $required) -PathType Leaf)) {
        throw "Bundled readpst license/provenance file is missing: $required"
    }
}

$requiredLicenses = @(
    "QT-SOURCE.md",
    "qtwebengine\LICENSE.Chromium",
    "qtwebengine\src\3rdparty\chromium\LICENSE",
    "python\Python-LICENSE.txt",
    "pyinstaller\bootloader-COPYING.txt"
)
foreach ($relative in $requiredLicenses) {
    if (-not (Test-Path -LiteralPath (Join-Path $LicenseRoot $relative) -PathType Leaf)) {
        throw "Required bundled license material is missing: $relative"
    }
}
$qtLicenseFiles = @(Get-ChildItem -LiteralPath (Join-Path $LicenseRoot "qt") -Recurse -File -ErrorAction SilentlyContinue)
if (-not ($qtLicenseFiles | Where-Object { $_.Name -match "(?i)^(?:LGPL|LICENSE\.LGPL).*3" } | Select-Object -First 1)) {
    throw "The Qt LGPL-3 license text is missing."
}
if (-not ($qtLicenseFiles | Where-Object { $_.Name -match "(?i)^(?:GPL|LICENSE\.GPL).*3" } | Select-Object -First 1)) {
    throw "The Qt GPL-3 license text is missing."
}
if (-not (Get-ChildItem -LiteralPath (Join-Path $LicenseRoot "qtwebengine") -Recurse -File -Filter "README.chromium" -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    throw "QtWebEngine Chromium third-party notices are missing."
}

$selfCheckOutput = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString("N") + ".json")
try {
    $selfCheck = Start-Process -FilePath $executable `
        -ArgumentList ('self-check --require-keyring --output "{0}"' -f $selfCheckOutput) `
        -Wait -PassThru
    if ($selfCheck.ExitCode -ne 0) { throw "Frozen self-check failed with exit code $($selfCheck.ExitCode)." }
    if (-not (Test-Path -LiteralPath $selfCheckOutput -PathType Leaf)) { throw "Frozen self-check did not write its JSON report." }
    $report = Get-Content -LiteralPath $selfCheckOutput -Raw | ConvertFrom-Json
    if (-not $report.passed -or -not $report.checks) { throw "Frozen self-check report is incomplete or unsuccessful." }
}
finally {
    Remove-Item -LiteralPath $selfCheckOutput -Force -ErrorAction SilentlyContinue
}

Write-Host "Frozen executable, long-path manifest, readpst hashes, license bundle, and required self-check are valid."