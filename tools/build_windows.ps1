[CmdletBinding()]
param(
    [string]$Msys2Root = "C:\msys64",
    [string]$ZstdPath = "",
    [string[]]$QtLicenseSource = @(),
    [string]$QtWebEngineSource = "",
    [string]$QtWebEngineSha256 = "",
    [switch]$CompileInstaller,
    [string]$InnoSetupCompiler = ""
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $repositoryRoot
try {
    function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code $LASTEXITCODE`: $Executable"
        }
    }

    if (-not $QtLicenseSource -or [string]::IsNullOrWhiteSpace($QtWebEngineSource) -or [string]::IsNullOrWhiteSpace($QtWebEngineSha256)) {
        throw "Provide version-locked Qt license archives and the QtWebEngine archive/hash from the Qt source lock."
    }
    foreach ($source in $QtLicenseSource) {
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Qt license source archive is missing: $source" }
    }
    if (-not (Test-Path -LiteralPath $QtWebEngineSource -PathType Leaf)) { throw "QtWebEngine source archive is missing: $QtWebEngineSource" }

    Invoke-Checked "uv" @("sync", "--locked", "--group", "build")
    $fetchArguments = @("-NoProfile", "-File", (Join-Path $PSScriptRoot "fetch_readpst.ps1"), "-Msys2Root", $Msys2Root)
    if (-not [string]::IsNullOrWhiteSpace($ZstdPath)) { $fetchArguments += @("-ZstdPath", $ZstdPath) }
    Invoke-Checked (Join-Path $PSHOME "pwsh.exe") $fetchArguments

    $verifyReadpstArguments = @("-NoProfile", "-File", (Join-Path $PSScriptRoot "verify_readpst_bundle.ps1"))
    if (-not [string]::IsNullOrWhiteSpace($ZstdPath)) { $verifyReadpstArguments += @("-ZstdPath", $ZstdPath) }
    Invoke-Checked (Join-Path $PSHOME "pwsh.exe") $verifyReadpstArguments

    $licenseOutput = Join-Path $repositoryRoot "build\licenses"
    if (Test-Path -LiteralPath $licenseOutput) { Remove-Item -LiteralPath $licenseOutput -Recurse -Force }

    $licenseArguments = @(
        "run", "python", "tools/collect_licenses.py",
        "--output-dir", "build/licenses",
        "--inventory", "THIRD-PARTY-LICENSES.md",
        "--qtwebengine-source", $QtWebEngineSource,
        "--qtwebengine-sha256", $QtWebEngineSha256
    )
    foreach ($source in $QtLicenseSource) { $licenseArguments += @("--qt-license-source", $source) }
    Invoke-Checked "uv" $licenseArguments

    Invoke-Checked "uv" @(
        "run", "pyinstaller", "--noconfirm",
        "--workpath", "build/pyinstaller",
        "--distpath", "dist",
        "packaging/pyinstaller/mail-dock.spec"
    )

    $qtSourceBuilder = Join-Path $PSScriptRoot "build_qt_sources.ps1"
    if (-not (Test-Path -LiteralPath $qtSourceBuilder -PathType Leaf)) {
        throw "Qt corresponding-source stage is not implemented yet: $qtSourceBuilder"
    }
    Invoke-Checked (Join-Path $PSHOME "pwsh.exe") @(
        "-NoProfile", "-File", $qtSourceBuilder,
        "-DistributionRoot", (Join-Path $repositoryRoot "dist\mail-dock\_internal"),
        "-SourceOutputRoot", (Join-Path $repositoryRoot "build\sources\qt"),
        "-LicenseOutputPath", (Join-Path $repositoryRoot "build\licenses\QT-SOURCE.md")
    )

    $bundleLicenseRoot = Join-Path $repositoryRoot "dist\mail-dock\_internal\licenses"
    New-Item -ItemType Directory -Force -Path $bundleLicenseRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $repositoryRoot "build\licenses\QT-SOURCE.md") -Destination $bundleLicenseRoot

    $verifyArguments = @("-NoProfile", "-File", (Join-Path $PSScriptRoot "verify_release_bundle.ps1"))
    Invoke-Checked (Join-Path $PSHOME "pwsh.exe") $verifyArguments

    if ($CompileInstaller) {
        if ([string]::IsNullOrWhiteSpace($InnoSetupCompiler)) {
            $compiler = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
            if ($null -eq $compiler) { throw "ISCC.exe was not found. Set -InnoSetupCompiler." }
            $InnoSetupCompiler = $compiler.Source
        }
        $installerScript = Join-Path $repositoryRoot "packaging\inno\mail-dock.iss"
        if (-not (Test-Path -LiteralPath $installerScript -PathType Leaf)) { throw "Inno Setup script is not available yet: $installerScript" }
        $appVersion = & uv run python -c "from mail_dock import __version__; print(__version__)"
        if ($LASTEXITCODE -ne 0) { throw "Could not read the application version from mail_dock.__version__." }
        $appVersion = $appVersion.Trim()
        if ($appVersion -notmatch '^\d+\.\d+\.\d+$') { throw "Unsupported application version: $appVersion" }
        Invoke-Checked $InnoSetupCompiler @("/Qp", "/DAppVersion=$appVersion", $installerScript, "/O$repositoryRoot\dist")
    }
}
finally {
    Pop-Location
}