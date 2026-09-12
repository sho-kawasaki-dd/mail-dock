[CmdletBinding()]
param(
    [string]$Msys2Root = "C:\msys64",
    [string]$OutputRoot = "",
    [string]$SourceCommit = "cc600ee98c4ed23b8ab0bc2cf6b6c6e9cb587e89",
    [string]$PackageName = "mingw-w64-ucrt-x86_64-libpst"
)

$ErrorActionPreference = "Stop"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $repositoryRoot "vendor\readpst"
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
$Msys2Root = [System.IO.Path]::GetFullPath($Msys2Root)

$pacman = Join-Path $Msys2Root "usr\bin\pacman.exe"
$ucrtBin = Join-Path $Msys2Root "ucrt64\bin"
$packageRoot = Join-Path $Msys2Root "ucrt64"
$manifestPath = Join-Path $OutputRoot "readpst.exe.manifest"

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $output = & $FilePath @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $details = ([string[]]$output -join [Environment]::NewLine).Trim()
        throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')`n$details"
    }
    return ([string[]]$output -join [Environment]::NewLine)
}

function Get-PackageField {
    param(
        [Parameter(Mandatory = $true)][string]$Metadata,
        [Parameter(Mandatory = $true)][string]$Field
    )

    $match = [regex]::Match($Metadata, "(?m)^$([regex]::Escape($Field))\s*:\s*(.+)$")
    if (-not $match.Success) {
        throw "Package metadata does not contain '$Field'."
    }
    return $match.Groups[1].Value.Trim()
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Get-MsysPath {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    $relative = $WindowsPath.Substring($Msys2Root.Length).TrimStart("\", "/")
    return "/" + $relative.Replace("\", "/")
}

function Get-OwnerPackage {
    param([Parameter(Mandatory = $true)][string]$WindowsPath)

    $owner = Invoke-NativeChecked $pacman @("-Qo", (Get-MsysPath $WindowsPath))
    $match = [regex]::Match($owner, "owned by\s+(\S+)\s+(\S+)")
    if (-not $match.Success) {
        throw "Could not determine the MSYS2 package owning '$WindowsPath'."
    }
    return $match.Groups[1].Value
}

function Get-PackageRecord {
    param([Parameter(Mandatory = $true)][string]$Name)

    $metadata = Invoke-NativeChecked $pacman @("-Qi", $Name)
    [ordered]@{
        name = $Name
        version = Get-PackageField $metadata "Version"
        url = Get-PackageField $metadata "URL"
        licenses = Get-PackageField $metadata "Licenses"
    }
}

if (-not (Test-Path -LiteralPath $pacman -PathType Leaf)) {
    throw "MSYS2 pacman was not found: $pacman"
}
if (-not (Test-Path -LiteralPath $ucrtBin -PathType Container)) {
    throw "MSYS2 UCRT64 bin directory was not found: $ucrtBin"
}
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "The authored readpst manifest was not found: $manifestPath"
}

$packageMetadata = Invoke-NativeChecked $pacman @("-Qi", $PackageName)
$packageRecord = [ordered]@{
    name = $PackageName
    version = Get-PackageField $packageMetadata "Version"
    url = Get-PackageField $packageMetadata "URL"
    licenses = Get-PackageField $packageMetadata "Licenses"
}
$expectedPackageVersion = "0.6.76.r79.gcc600ee-1"
if ($packageRecord.version -ne $expectedPackageVersion) {
    throw "Unexpected $PackageName version '$($packageRecord.version)'. Update SourceCommit and the recorded provenance before refreshing this bundle."
}
$sourceUrl = "https://github.com/pst-format/libpst/archive/$SourceCommit.tar.gz"

$requiredBinaries = @("readpst.exe", "lspst.exe")
$requiredDlls = @(
    "libbz2-1.dll",
    "libffi-8.dll",
    "libgcc_s_seh-1.dll",
    "libgio-2.0-0.dll",
    "libglib-2.0-0.dll",
    "libgmodule-2.0-0.dll",
    "libgobject-2.0-0.dll",
    "libgsf-1-114.dll",
    "libiconv-2.dll",
    "libintl-8.dll",
    "libpcre2-8-0.dll",
    "libpst-4.dll",
    "libstdc++-6.dll",
    "libsystre-0.dll",
    "libtre-5.dll",
    "libwinpthread-1.dll",
    "libxml2-16.dll",
    "zlib1.dll"
)

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
foreach ($name in $requiredBinaries + $requiredDlls) {
    $sourcePath = Join-Path $ucrtBin $name
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required MSYS2 artifact was not found: $sourcePath"
    }
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $OutputRoot $name) -Force
}

$copying = Get-ChildItem -LiteralPath (Join-Path $packageRoot "share\doc") -Filter COPYING -Recurse -File |
    Where-Object { $_.FullName -match "libpst" } |
    Select-Object -First 1
if ($null -eq $copying) {
    throw "The libpst COPYING file was not found below $packageRoot."
}
Copy-Item -LiteralPath $copying.FullName -Destination (Join-Path $OutputRoot "COPYING") -Force

$sourceArchive = Join-Path $OutputRoot "libpst-$SourceCommit.tar.gz"
Invoke-WebRequest -Uri $sourceUrl -OutFile $sourceArchive -UseBasicParsing
if (-not (Test-Path -LiteralPath $sourceArchive -PathType Leaf)) {
    throw "The libpst corresponding source archive was not downloaded: $sourceUrl"
}

$readpstPath = Join-Path $OutputRoot "readpst.exe"
$beforeManifestHash = Get-Sha256 $readpstPath
$mt = Get-Command mt.exe -ErrorAction SilentlyContinue
if ($null -eq $mt) {
    throw "mt.exe was not found. Install the Windows SDK before fetching readpst."
}
$outputResource = "$readpstPath;#1"
& $mt.Source -manifest $manifestPath "-outputresource:$outputResource"
if ($LASTEXITCODE -ne 0) {
    throw "mt.exe failed to apply the readpst manifest."
}
$afterManifestHash = Get-Sha256 $readpstPath

$artifactPaths = @(
    $requiredBinaries + $requiredDlls |
        ForEach-Object { Join-Path $OutputRoot $_ }
)
$artifactPaths += @(
    (Join-Path $OutputRoot "COPYING"),
    $sourceArchive,
    $manifestPath
)

$dependencyPackages = [ordered]@{}
foreach ($path in ($requiredBinaries + $requiredDlls | ForEach-Object { Join-Path $ucrtBin $_ })) {
    $owner = Get-OwnerPackage $path
    if (-not $dependencyPackages.Contains($owner)) {
        $dependencyPackages[$owner] = Get-PackageRecord $owner
    }
}

$artifacts = foreach ($path in $artifactPaths) {
    [ordered]@{
        path = [System.IO.Path]::GetRelativePath($OutputRoot, $path).Replace("\", "/")
        sha256 = Get-Sha256 $path
        size_bytes = (Get-Item -LiteralPath $path).Length
    }
}
$manifest = [ordered]@{
    schema_version = 1
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    package = $packageRecord
    dependencies = @($dependencyPackages.Values)
    source = [ordered]@{
        repository = "https://github.com/pst-format/libpst"
        commit = $SourceCommit
        url = $sourceUrl
        archive = [System.IO.Path]::GetFileName($sourceArchive)
        sha256 = Get-Sha256 $sourceArchive
    }
    readpst_manifest = [ordered]@{
        path = "readpst.exe.manifest"
        sha256 = Get-Sha256 $manifestPath
        readpst_sha256_before = $beforeManifestHash
        readpst_sha256_after = $afterManifestHash
    }
    artifacts = @($artifacts)
}
$manifestJsonPath = Join-Path $OutputRoot "readpst-artifacts.json"
$manifest | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $manifestJsonPath -Encoding utf8

$checksumLines = foreach ($artifact in $artifacts) {
    "{0}  {1}" -f $artifact.sha256, $artifact.path
}
$checksumLines += "{0}  readpst-artifacts.json" -f (Get-Sha256 $manifestJsonPath)
$checksumLines | Set-Content -LiteralPath (Join-Path $OutputRoot "SHA256SUMS") -Encoding ascii

Write-Host "Fetched $PackageName $($packageRecord.version)"
Write-Host "readpst SHA-256 before manifest patch: $beforeManifestHash"
Write-Host "readpst SHA-256 after manifest patch:  $afterManifestHash"
Write-Host "Corresponding source: $sourceUrl"
Write-Host "Artifacts written to: $OutputRoot"