param(
    [string]$Base = "tools/.pst_poc"
)

$ErrorActionPreference = "Stop"
$rp = (Resolve-Path "./vendor/readpst/readpst.exe").Path
$baseDir = (Resolve-Path $Base).Path
$results = @()

foreach ($f in Get-ChildItem (Join-Path $baseDir "corrupt") -Filter *.pst | Sort-Object Name) {
    $out = Join-Path $baseDir ("out_" + $f.BaseName)
    New-Item -ItemType Directory -Force -Path $out | Out-Null
    $so = Join-Path $baseDir "$($f.BaseName).out.txt"
    $se = Join-Path $baseDir "$($f.BaseName).err.txt"
    $readpstArgs = @("-e", "-t", "e", "-8", "-j", "0", "-q", "-C", "cp932", "-o", $out, $f.FullName)
    $p = Start-Process -FilePath $rp -ArgumentList $readpstArgs -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $so -RedirectStandardError $se
    $errText = [string](Get-Content $se -Raw -ErrorAction SilentlyContinue)
    $outText = [string](Get-Content $so -Raw -ErrorAction SilentlyContinue)
    $results += [pscustomobject]@{
        File   = $f.Name
        Exit   = $p.ExitCode
        Files  = (Get-ChildItem $out -Recurse -File -ErrorAction SilentlyContinue).Count
        Stdout = ($outText -replace "\r?\n", " / ").Trim()
        Stderr = ($errText -replace "\r?\n", " / ").Trim()
    }
}

$results | Format-List
