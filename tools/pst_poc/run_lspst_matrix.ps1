param(
    [string]$Base = "tools/.pst_poc"
)

$ErrorActionPreference = "Stop"
$lp = (Resolve-Path "./vendor/readpst/lspst.exe").Path
$baseDir = (Resolve-Path $Base).Path
$results = @()

foreach ($f in Get-ChildItem (Join-Path $baseDir "corrupt") -Filter *.pst | Sort-Object Name) {
    $so = Join-Path $baseDir "lspst_$($f.BaseName).out.txt"
    $se = Join-Path $baseDir "lspst_$($f.BaseName).err.txt"
    $p = Start-Process -FilePath $lp -ArgumentList @($f.FullName) -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $so -RedirectStandardError $se
    $outText = [string](Get-Content $so -Raw -ErrorAction SilentlyContinue)
    $errText = [string](Get-Content $se -Raw -ErrorAction SilentlyContinue)
    $results += [pscustomobject]@{
        File   = $f.Name
        Exit   = $p.ExitCode
        Stdout = ($outText -replace "\r?\n", " / ").Trim()
        Stderr = ($errText -replace "\r?\n", " / ").Trim()
    }
}

$results | Format-List
