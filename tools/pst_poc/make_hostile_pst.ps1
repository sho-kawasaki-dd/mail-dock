# Phase 4.5 グループA PoC 用: 敵性フォルダ名を含む検証用PSTを Outlook COM で生成する。
#
# Outlook の現在のMAPIプロファイルへ一時的にPSTを追加し、フォルダとメールを作成したあと
# 必ず RemoveStore でプロファイルから切り離す。生成された .pst ファイルは残る。
#
# 使い方:
#   pwsh -NoProfile -File tools/pst_poc/make_hostile_pst.ps1 -Output C:\pstpoc\hostile.pst

param(
    [string]$Output = "C:\pstpoc\hostile.pst"
)

$ErrorActionPreference = "Stop"

$olStoreUnicode = 3

$outDir = Split-Path -Parent $Output
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
if (Test-Path $Output) { Remove-Item -LiteralPath $Output -Force }

# NFC / NFD で同一に見えるフォルダ名の衝突を作る
$nfc = [string][char]0x304C + "test"                        # が(合成済み)
$nfd = [string][char]0x304B + [string][char]0x3099 + "test" # か + 濁点

# Id は ASCII のみ。抽出後に .eml の Subject から出力ディレクトリを逆引きするために使う。
$candidates = @(
    @{ Id = "colon";        Parent = ""; Name = 'a:b' }
    @{ Id = "backslash";    Parent = ""; Name = 'a\b' }
    @{ Id = "slash";        Parent = ""; Name = 'a/b' }
    @{ Id = "asterisk";     Parent = ""; Name = 'a*b' }
    @{ Id = "question";     Parent = ""; Name = 'a?b' }
    @{ Id = "quote";        Parent = ""; Name = 'a"b' }
    @{ Id = "lt";           Parent = ""; Name = 'a<b' }
    @{ Id = "gt";           Parent = ""; Name = 'a>b' }
    @{ Id = "pipe";         Parent = ""; Name = 'a|b' }
    @{ Id = "reserved_con"; Parent = ""; Name = 'CON' }
    @{ Id = "reserved_prn"; Parent = ""; Name = 'PRN' }
    @{ Id = "reserved_nul"; Parent = ""; Name = 'NUL' }
    @{ Id = "reserved_aux"; Parent = ""; Name = 'AUX' }
    @{ Id = "reserved_com1"; Parent = ""; Name = 'COM1' }
    @{ Id = "reserved_lpt1"; Parent = ""; Name = 'LPT1' }
    @{ Id = "reserved_ext"; Parent = ""; Name = 'CON.txt' }
    @{ Id = "trail_dot";    Parent = ""; Name = 'trail.' }
    @{ Id = "trail_space";  Parent = ""; Name = 'trail ' }
    @{ Id = "lead_space";   Parent = ""; Name = ' lead' }
    @{ Id = "dot";          Parent = ""; Name = '.' }
    @{ Id = "dotdot";       Parent = ""; Name = '..' }
    @{ Id = "traversal";    Parent = ""; Name = '..\..\evil' }
    @{ Id = "abs_path";     Parent = ""; Name = 'C:\Windows\Temp\evil' }
    @{ Id = "drive_rel";    Parent = ""; Name = 'C:evil' }
    @{ Id = "unc";          Parent = ""; Name = '\\server\share\evil' }
    @{ Id = "ads";          Parent = ""; Name = 'note.txt:hidden' }
    @{ Id = "tilde";        Parent = ""; Name = 'PROGRA~1' }
    @{ Id = "long_name";    Parent = ""; Name = ('N' * 250) }
    @{ Id = "nfc";          Parent = ""; Name = $nfc }
    @{ Id = "nfd";          Parent = ""; Name = $nfd }
    @{ Id = "dup_parent_a"; Parent = ""; Name = 'ParentA' }
    @{ Id = "dup_parent_b"; Parent = ""; Name = 'ParentB' }
    @{ Id = "dup_in_a";     Parent = "ParentA"; Name = 'SameName' }
    @{ Id = "dup_in_b";     Parent = "ParentB"; Name = 'SameName' }
)

$ol = New-Object -ComObject Outlook.Application
$ns = $ol.GetNamespace("MAPI")

$ns.AddStoreEx($Output, $olStoreUnicode)

$store = $null
foreach ($s in $ns.Stores) {
    if ($s.FilePath -and ($s.FilePath -ieq $Output)) { $store = $s }
}
if ($null -eq $store) { throw "store not found after AddStoreEx: $Output" }

$results = @()
try {
    $root = $store.GetRootFolder()
    $created = @{}

    foreach ($c in $candidates) {
        $parentFolder = $root
        if ($c.Parent) {
            if (-not $created.ContainsKey($c.Parent)) { continue }
            $parentFolder = $created[$c.Parent]
        }
        $requested = [string]$c.Name
        try {
            $f = $parentFolder.Folders.Add($requested)
            $actual = [string]$f.Name
            $created[$actual] = $f

            $m = $f.Items.Add("IPM.Note")
            $m.Subject = "PSTPOC-$($c.Id)"
            $m.Body = "marker for $($c.Id)"
            $m.Save()

            $results += [pscustomobject]@{
                Id        = $c.Id
                Requested = $requested
                Created   = $true
                Actual    = $actual
                Changed   = ($actual -cne $requested)
                Error     = ""
            }
        }
        catch {
            $results += [pscustomobject]@{
                Id        = $c.Id
                Requested = $requested
                Created   = $false
                Actual    = ""
                Changed   = $false
                Error     = ($_.Exception.Message -replace "\r?\n", " ").Trim()
            }
        }
    }
}
finally {
    $ns.RemoveStore($store.GetRootFolder())
    [Runtime.InteropServices.Marshal]::ReleaseComObject($ol) | Out-Null
}

$results | Format-Table -AutoSize Id, Created, Changed, Requested, Actual, Error | Out-String -Width 400
Write-Host "created pst: $Output ($((Get-Item $Output).Length) bytes)"
