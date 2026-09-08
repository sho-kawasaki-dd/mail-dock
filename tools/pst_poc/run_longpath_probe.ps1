param(
    [Parameter(Mandatory = $true)][string]$Pst,
    [Parameter(Mandatory = $true)][string]$OutRoot,
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = "Stop"
$rp = (Resolve-Path "./vendor/readpst/readpst.exe").Path
$pstPath = (Resolve-Path $Pst).Path

New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null
$outRootFull = (Resolve-Path $OutRoot).Path
Write-Host "readpst -o path length: $($outRootFull.Length)"

$so = Join-Path $env:TEMP "readpst_longpath.out.txt"
$se = Join-Path $env:TEMP "readpst_longpath.err.txt"
$readpstArgs = @("-e", "-t", "e", "-8", "-j", "0", "-q", "-C", "cp932", "-o", $outRootFull, $pstPath)
$p = Start-Process -FilePath $rp -ArgumentList $readpstArgs -NoNewWindow -PassThru `
    -RedirectStandardOutput $so -RedirectStandardError $se

if (-not $p.WaitForExit($TimeoutSec * 1000)) {
    Write-Host "timeout after ${TimeoutSec}s -> terminating"
    $p.Kill($true)
    $p.WaitForExit()
    $timedOut = $true
}
else {
    $timedOut = $false
    Write-Host "exited with code $($p.ExitCode)"
}

$paths = Get-ChildItem -LiteralPath $outRootFull -Recurse -Force -ErrorAction SilentlyContinue
$maxAbs = 0
$maxRel = 0
$maxRelPath = ""
foreach ($item in $paths) {
    if ($item.FullName.Length -gt $maxAbs) { $maxAbs = $item.FullName.Length }
    $rel = $item.FullName.Substring($outRootFull.Length).TrimStart('\')
    if ($rel.Length -gt $maxRel) { $maxRel = $rel.Length; $maxRelPath = $rel }
}

[pscustomobject]@{
    TimedOut      = $timedOut
    ExitCode      = if ($timedOut) { $null } else { $p.ExitCode }
    OutRootLength = $outRootFull.Length
    Entries       = $paths.Count
    Dirs          = ($paths | Where-Object { $_.PSIsContainer }).Count
    Files         = ($paths | Where-Object { -not $_.PSIsContainer }).Count
    MaxAbsLength  = $maxAbs
    MaxRelLength  = $maxRel
    Stdout        = ([string](Get-Content $so -Raw -ErrorAction SilentlyContinue) -replace "\r?\n", " / ").Trim()
    Stderr        = ([string](Get-Content $se -Raw -ErrorAction SilentlyContinue) -replace "\r?\n", " / ").Trim()
} | Format-List

Write-Host "deepest relative path: $maxRelPath"
