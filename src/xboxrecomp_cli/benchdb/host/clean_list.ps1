#@ benchdb/host/clean_list.ps1: clean_list.sh for the PC under Windows,
#@ after clean_lib.ps1 and $Roots, $Sweep (1: list the root's children)
#@ and $Paths. Read-only. The same lines as clean_list.sh; uid is '-'.
Out-Line @('H', '-', "$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())")
for ($i = 0; $i -lt $Roots.Count; $i++) {
    $r = [IO.Path]::GetFullPath($Roots[$i])
    if ([IO.Directory]::Exists($r)) {
        $dr = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($r))
        Out-Line @('T', "$i", $r, "$([int64]($dr.TotalSize / 1024))", "$([int64]($dr.AvailableFreeSpace / 1024))")
    } else {
        Out-Line @('G', "$i")
    }
}
function Write-Entry([string]$p, [string]$root) {
    if ($p -match "[`t`n]") { Out-Line @('B', ($p -replace "[`t`n]", '?')); return }
    $res, $via = Resolve-Leaf $p $root
    if (-not (Test-Path -LiteralPath $res)) { Out-Line @('M', $p); return }
    $a = [IO.File]::GetAttributes($res)
    $ty = if (($a -band $RP) -ne 0) { 'l' } elseif (($a -band $DIR) -ne 0) { 'd' } else { 'f' }
    $mt, $b = Measure-Tree $res
    $real = if ($ty -eq 'l') { '-' } elseif ($via) { "reparse:$via" } else { $res }
    Out-Line @('E', $p, $res, $real, $ty, '-', "$mt", "$([int64]($b / 1024))")
    if ($ty -ne 'd') { return }
    # Each bench-logs\<stamp> at most four levels down, by the same walk.
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($res, 0))
    while ($stack.Count) {
        $d, $depth = $stack.Pop()
        foreach ($e in [IO.Directory]::EnumerateDirectories($d)) {
            if (([IO.File]::GetAttributes($e) -band $RP) -ne 0) { continue }
            $n = [IO.Path]::GetFileName($e)
            if ([IO.Path]::GetFileName($d) -eq 'bench-logs' -and $n -match '^[0-9]{8}-[0-9]{6}(-[A-Za-z0-9_.-]+)?$') {
                $ri = Join-Path $e 'run-info.txt'
                $sha = if ([IO.File]::Exists($ri)) { (Get-FileHash -Algorithm SHA256 -LiteralPath $ri).Hash.ToLower() } else { '-' }
                $ex = if ([IO.File]::Exists((Join-Path $e 'exit-code'))) { '1' } else { '0' }
                Out-Line @('S', $res, $n, $sha, $ex)
                $fs = New-Object System.Collections.Stack
                $fs.Push($e)
                while ($fs.Count) {
                    $q = $fs.Pop()
                    foreach ($x in [IO.Directory]::EnumerateFileSystemEntries($q)) {
                        $xa = [IO.File]::GetAttributes($x)
                        $rel = $x.Substring($e.Length + 1).Replace('\', '/')
                        if (($xa -band $RP) -ne 0) { continue }
                        if (($xa -band $DIR) -ne 0) { if ($rel -ne 'frames') { $fs.Push($x) } }
                        else { Out-Line @('F', $res, $n, $rel) }
                    }
                }
            } elseif ($depth -lt 3) {
                $stack.Push(@($e, ($depth + 1)))
            }
        }
    }
}
foreach ($p in $Paths) {
    $root = ($Roots | Where-Object { Test-Under ([IO.Path]::GetFullPath($p)) ([IO.Path]::GetFullPath($_)) } | Select-Object -First 1)
    Write-Entry $p $(if ($root) { [IO.Path]::GetFullPath($root) } else { [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($p)) })
}
for ($i = 0; $i -lt $Roots.Count; $i++) {
    $r = [IO.Path]::GetFullPath($Roots[$i])
    if ($Sweep[$i] -ne 1 -or -not [IO.Directory]::Exists($r)) { continue }
    foreach ($n in [IO.Directory]::EnumerateFileSystemEntries($r)) { Write-Entry $n $r }
}
