#@ benchdb/host/clean_apply.ps1: clean_apply.sh for the PC under Windows,
#@ after clean_lib.ps1 and $Lock (D:\xbr\.bench-lock), $Owner (the lock's
#@ text before its pid), $Roots, $Never, $NeverSelf, $Bench (as in
#@ clean_apply.sh) and $Items (one hashtable per path:
#@ p, r the root's index, pat, m the planned newest mtime, age in seconds).
#@ Prints deleted|skipped|failed <tab> path [<tab> why] per path.
# The lock is the runs' (run_game.ps1): CreateNew with this text, ending
# in this PowerShell's pid and start time; a takeover only of a stale
# bench lock, in place under an exclusive handle; the lock removed only
# while it holds this text. So a run never takes over a lock clean holds,
# and clean never takes over one taken by hand.
$me = Get-Process -Id $PID
$mine = "$Owner pid=$PID start=$($me.StartTime.ToUniversalTime().Ticks)"
function Test-StaleLock([string]$text) {
    if ($text -notmatch '^bench .* pid=(\d+) start=(\d+)\s*$') { return $false }
    $h = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
    return -not $h -or "$($h.StartTime.ToUniversalTime().Ticks)" -ne $Matches[2]
}
$held = $false
$t0 = Get-Date
$said = $false
New-Item -ItemType Directory -Force -Path (Split-Path $Lock) | Out-Null
while (-not $held) {
    try {
        $s = [IO.File]::Open($Lock, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $bytes = [Text.Encoding]::UTF8.GetBytes("$mine`n")
        $s.Write($bytes, 0, $bytes.Length)
        $s.Close()
        $held = $true
        break
    } catch [System.IO.IOException] {
        $text = "$(Get-Content -Raw -LiteralPath $Lock -ErrorAction SilentlyContinue)".Trim()
        if ($text -and (Test-StaleLock $text)) {
            try {
                $s = [IO.File]::Open($Lock, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                try {
                    $buf = New-Object byte[] $s.Length
                    $n = $s.Read($buf, 0, $buf.Length)
                    if ([Text.Encoding]::UTF8.GetString($buf, 0, $n).Trim() -eq $text) {
                        $bytes = [Text.Encoding]::UTF8.GetBytes("$mine`n")
                        $s.SetLength(0)
                        $s.Write($bytes, 0, $bytes.Length)
                        $held = $true
                    }
                } finally { $s.Close() }
            } catch {
            }
            if ($held) { [Console]::Error.WriteLine("benchdb: took over ${Lock}: its holder is gone ($text)"); break }
        }
        if (-not $said) { [Console]::Error.WriteLine("benchdb: clean waiting for $Lock, held by: $text"); $said = $true }
        if (((Get-Date) - $t0).TotalSeconds -ge 3600) { exit 75 }
        Start-Sleep -Seconds 5
    }
}
function Remove-Tree([string]$p) {
    $a = [IO.File]::GetAttributes($p)
    if (($a -band $RP) -ne 0) {
        # A link is deleted as a link and never entered; its read-only flag
        # is never touched, so the target keeps its attributes.
        if (($a -band $DIR) -ne 0) { [IO.Directory]::Delete($p, $false) } else { [IO.File]::Delete($p) }
        return
    }
    if (($a -band $DIR) -eq 0) {
        if (($a -band $RO) -ne 0) { [IO.File]::SetAttributes($p, ($a -band (-bnot $RO))) }
        [IO.File]::Delete($p)
        return
    }
    foreach ($e in @([IO.Directory]::EnumerateFileSystemEntries($p))) { Remove-Tree $e }
    if (($a -band $RO) -ne 0) { [IO.File]::SetAttributes($p, ($a -band (-bnot $RO))) }
    [IO.Directory]::Delete($p, $false)
}
$fails = 0
try {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    foreach ($it in $Items) {
        $p = $it.p
        if ($p.EndsWith('\') -or $p.EndsWith('/') -or $p -match '(^|[\\/])\.\.([\\/]|$)') { Out-Line @('skipped', $p, 'a trailing \ or a .. in the path'); continue }
        $root = [IO.Path]::GetFullPath($Roots[$it.r]).TrimEnd('\')
        $res, $via = Resolve-Leaf $p $root
        if ($via) { Out-Line @('skipped', $p, "a reparse point in its parent: $via"); continue }
        if ($res.Equals($root, 'OrdinalIgnoreCase') -or -not (Test-Under $res $root)) { Out-Line @('skipped', $p, "$res is not below the root $root"); continue }
        # The whole path below the root with / separators, as plan matched it.
        $rel = $res.Substring($root.Length + 1).Replace('\', '/')
        if (-not [regex]::IsMatch($rel, '^(?:' + $it.pat + ')$')) { Out-Line @('skipped', $p, "$rel does not match the root's pattern"); continue }
        $bad = $Never | Where-Object { (Test-Under $res ([IO.Path]::GetFullPath($_))) -or (Test-Under ([IO.Path]::GetFullPath($_)) $res) } | Select-Object -First 1
        if (-not $bad) { $bad = $NeverSelf | Where-Object { Test-Under ([IO.Path]::GetFullPath($_)) $res } | Select-Object -First 1 }
        if (-not $bad) {
            $bad = $Bench | Where-Object {
                $b = [IO.Path]::GetFullPath($_).TrimEnd('\')
                (Test-Under $b $res) -or ((Test-Under $res $b) -and -not [regex]::IsMatch($res.Substring($b.Length + 1), '^[^\\]+\\runs\\[^\\]+', 'IgnoreCase'))
            } | Select-Object -First 1
        }
        if ($bad) { Out-Line @('skipped', $p, "never-path $bad"); continue }
        if (-not (Test-Path -LiteralPath $res)) { Out-Line @('skipped', $p, 'gone'); continue }
        $a = [IO.File]::GetAttributes($res)
        if (($a -band $RP) -eq 0) {
            $mt, $b = Measure-Tree $res
            if ("$mt" -ne "$($it.m)") { Out-Line @('skipped', $p, "changed since the plan (newest mtime $mt, planned $($it.m))"); continue }
            if (($now - $mt) -lt $it.age) { Out-Line @('skipped', $p, "written within $([int]($it.age / 86400)) days"); continue }
        }
        try {
            if (($a -band $RP) -eq 0 -and ($a -band $DIR) -ne 0) {
                # Out of a ship's way first, as clean_apply.sh does: a ship
                # after the move makes a fresh dir; one that wrote before it
                # shows in the re-read, and the dir goes back.
                $gone = Join-Path ([IO.Path]::GetDirectoryName($res)) ('.' + [IO.Path]::GetFileName($res) + '.deleting.' + $PID)
                if (Test-Path -LiteralPath $gone) { throw "$gone is in the way" }
                [IO.Directory]::Move($res, $gone)
                $mt, $b = Measure-Tree $gone
                if ("$mt" -ne "$($it.m)") {
                    if (Test-Path -LiteralPath $res) { throw "shipped to during the clean; left as $gone" }
                    [IO.Directory]::Move($gone, $res)
                    Out-Line @('skipped', $p, "shipped to during the clean (newest mtime $mt)")
                    continue
                }
                Remove-Tree $gone
            } else {
                Remove-Tree $res
            }
            Out-Line @('deleted', $p)
        } catch {
            Out-Line @('failed', $p, ("$($_.Exception.Message)" -replace "[`t`r`n]", ' '))
            $fails++
        }
    }
} finally {
    if ($held -and "$(Get-Content -Raw -LiteralPath $Lock -ErrorAction SilentlyContinue)".Trim() -eq $mine) {
        Remove-Item -LiteralPath $Lock
    }
}
if ($fails) { exit 1 }
