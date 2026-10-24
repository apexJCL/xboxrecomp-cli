# bench/host/windows/run_game.ps1: one game run on a Windows host
# (BENCH_HOST_OS=windows), the counterpart of run_game.sh. Leaves in the
# run's log dir what check_run_end reads: run-info.txt (limit:, flips:,
# busy:), exit-code (124 once the run reached its limit) and survived.
# Params (JSON): stamp, run_dir, log, exe_rel, provenance, game_files,
# env ([key, value], resolved by the controller), args, timeout (s, 0 =
# none), lock, lock_held, kill_game, expect_sha, owner (the lock's text),
# launch (launch.ps1's path).
# Exits 0 once the game ran (its own result is in exit-code), 75 when the
# lock stayed held for an hour, 76 when the exe is not the shipped one,
# anything else when nothing ran.
param([Parameter(Mandatory)][string]$Params)
$ErrorActionPreference = 'Stop'
$p = Get-Content -Raw -Encoding UTF8 -LiteralPath $Params | ConvertFrom-Json
Remove-Item -LiteralPath $Params
$run = $p.run_dir
$exe = Join-Path $run $p.exe_rel
$log = $p.log
if (-not (Test-Path -LiteralPath $exe)) { Write-Host "bench: no $exe; ship it first"; exit 1 }
if (-not (Test-Path -LiteralPath $p.game_files)) { Write-Host "bench: no game files at $($p.game_files) (BENCH_GAME_FILES)"; exit 1 }

# The game paces itself to the display: under 60 Hz every flips/s and
# frame-time verdict would be off, so refuse rather than measure it.
$hz = @(Get-CimInstance Win32_VideoController | Where-Object { $_.CurrentRefreshRate } | ForEach-Object { [int]$_.CurrentRefreshRate })
if ($hz.Count -and ($hz | Measure-Object -Maximum).Maximum -lt 60) {
    Write-Host "bench: the display runs at $($hz -join ', ') Hz; the bench needs 60 Hz or more"
    exit 2
}

# One game at a time on this PC: every agent's runs take the same lock
# file. CreateNew is atomic; the file names its holder, and only a run
# whose text it holds deletes it. The text ends in this PowerShell's pid
# and start time: a run killed with its ssh session never reaches its
# finally, and a later run takes over a bench lock whose holder is gone
# (a pid reused by another process has another start time). The takeover
# rewrites the file in place under an exclusive handle and never deletes
# it, so of two takers the second gets a sharing violation, then reads
# the first one's live text. A lock without the bench prefix was taken by
# hand and is only waited for.
$lockStream = $null
$waited = 0
$tookOver = $null
$me = Get-Process -Id $PID
$mine = "$($p.owner) pid=$PID start=$($me.StartTime.ToUniversalTime().Ticks)"
function Test-StaleLock([string]$text) {
    if ($text -notmatch '^bench .* pid=(\d+) start=(\d+)\s*$') { return $false }
    $h = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
    return -not $h -or "$($h.StartTime.ToUniversalTime().Ticks)" -ne $Matches[2]
}
if ($p.lock_held) {
    Write-Host "bench: $($p.lock) is held for this run's series (BENCH_LOCK_HELD=1)"
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path $p.lock) | Out-Null
    $t0 = Get-Date
    $said = $false
    while ($true) {
        try {
            $lockStream = [IO.File]::Open($p.lock, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
            $bytes = [Text.Encoding]::UTF8.GetBytes("$mine`n")
            $lockStream.Write($bytes, 0, $bytes.Length)
            $lockStream.Close()
            break
        } catch [System.IO.IOException] {
            $held = "$(Get-Content -Raw -LiteralPath $p.lock -ErrorAction SilentlyContinue)".Trim()
            if ($held -and (Test-StaleLock $held)) {
                try {
                    $s = [IO.File]::Open($p.lock, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                    try {
                        # Re-read under the handle: another taker may have
                        # written its own text since the read above.
                        $buf = New-Object byte[] $s.Length
                        $n = $s.Read($buf, 0, $buf.Length)
                        if ([Text.Encoding]::UTF8.GetString($buf, 0, $n).Trim() -eq $held) {
                            $bytes = [Text.Encoding]::UTF8.GetBytes("$mine`n")
                            $s.SetLength(0)
                            $s.Write($bytes, 0, $bytes.Length)
                            $tookOver = $held
                            # finally removes it only behind this.
                            $lockStream = $s
                        }
                    } finally { $s.Close() }
                } catch {
                    # A sharing violation (another taker) or no write access:
                    # wait as for a live holder, within the same hour.
                }
                if ($tookOver) {
                    Write-Host "bench: took over $($p.lock): its holder is gone ($held)"
                    break
                }
            }
            if (-not $said) {
                Write-Host "bench: waiting for $($p.lock), held by: $held"
                $said = $true
            }
            if (((Get-Date) - $t0).TotalSeconds -ge 3600) {
                Write-Host "bench: FAIL $($p.lock) was held for an hour by: $held"
                Write-Host "bench: if no run is going on, remove $($p.lock) on the host"
                exit 75
            }
            Start-Sleep -Seconds 5
        }
    }
    $waited = [int]((Get-Date) - $t0).TotalSeconds
    if ($said) { Write-Host "bench: got $($p.lock) after ${waited}s" }
}

$task = "xbr-bench-$($p.stamp)"
# Under the lock no other bench run is going on, so another xbr-bench task
# was left by a run killed before its finally.
Get-ScheduledTask -TaskName 'xbr-bench-*' -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -ne $task } | ForEach-Object {
    Write-Host "bench: removing a task left by a killed run: $($_.TaskName)"
    Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
try {
    # The exe this command shipped, checked under the lock: another ship to
    # the same run directory may have replaced it while this run waited.
    $sha = (Get-FileHash -LiteralPath $exe).Hash.ToLower()
    if ($p.expect_sha -and $sha -ne $p.expect_sha) {
        Write-Host "bench: FAIL $exe is sha256 $sha, not the shipped $($p.expect_sha); nothing ran"
        exit 76
    }
    # Every bench run holds the lock, so a game process now was started
    # outside the bench (the installed copy, or by hand): end it with
    # --kill-game, else refuse, as its window and timings would mix in.
    $name = [IO.Path]::GetFileNameWithoutExtension($exe)
    $games = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    New-Item -ItemType Directory -Force -Path $log, "$log\frames", "$log\save" | Out-Null
    # The shader cache lives in the base, not the run (game_env): the
    # toolkit leaves the cache off when its directory is missing.
    $p.env | Where-Object { $_[0] -eq 'RECOMP_DXBC_CACHE' } | ForEach-Object { New-Item -ItemType Directory -Force -Path $_[1] | Out-Null }
    if ($tookOver) {
        # The dead holder's game outlives its task (launch.ps1 started it):
        # a game of this name run from this base's directories (any of its
        # runs\<id>, the dead run's exe may differ) is that one, as only
        # bench runs start games there. Anything else stays under the
        # --kill-game rule below.
        $base = [IO.Path]::GetFullPath($run).TrimEnd('\')
        if ((Split-Path -Leaf (Split-Path $base)) -eq 'runs') { $base = Split-Path (Split-Path $base) }
        $under = $base + '\'
        $left = @($games | Where-Object { $_.Path -and $_.Path.StartsWith($under, [StringComparison]::OrdinalIgnoreCase) })
        if ($left.Count) {
            $left | Stop-Process -Force
            "bench: ended the game the dead lock holder left: $name pid $($left.Id -join ', ')" | Tee-Object -Append -FilePath "$log\warnings.txt" | Write-Host
            Start-Sleep -Seconds 2
            $games = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        }
    }
    if ($games.Count) {
        if (-not $p.kill_game) {
            Write-Host "bench: FAIL $name is already running (pid $($games.Id -join ', ')); --kill-game ends it"
            exit 1
        }
        $games | Stop-Process -Force
        "bench: ended a game outside the bench: $name pid $($games.Id -join ', ')" | Tee-Object -Append -FilePath "$log\warnings.txt" | Write-Host
        Start-Sleep -Seconds 2
    }
    $envLine = ($p.env | ForEach-Object { "$($_[0])=$($_[1])" }) -join ' '
    $limit = if ($p.timeout) { "$($p.timeout) s (the game quits itself; kill at +30 s)" } else { 'none' }
    $info = @(
        "host:   $env:COMPUTERNAME  $(Get-Date -Format s)"
        "os:     windows (native, no Proton)"
        "env:    $(if ($envLine) { $envLine } else { '(none)' })"
        "args:   $(if ($p.args.Count) { $p.args -join ' ' } else { '(none)' })"
        "limit:  $limit"
        "lock:   $($p.lock), waited ${waited}s"
        "$sha  $($p.exe_rel)"
    )
    $prov = Join-Path $run $p.provenance
    if (Test-Path -LiteralPath $prov) { $info += Get-Content -LiteralPath $prov | ForEach-Object { "built:  $_" } }
    else { $info += "built:  unknown sources ($($p.provenance) missing)" }
    Set-Content -LiteralPath "$log\run-info.txt" $info

    # An ssh session has no desktop: the game starts from a one-shot task
    # in the logged-on user's interactive session, which writes the pid and
    # then the exit code.
    $pidFile = "$log\launch.pid"; $doneFile = "$log\launch.done"; $lp = "$log\launch.json"
    @{ exe = $exe; cwd = $log; args = @($p.args); env = $p.env; pid_file = $pidFile; done_file = $doneFile } |
        ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $lp
    $load0 = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$($p.launch)`" -Params `"$lp`""
    # The token's own name: under sshd USERDOMAIN reads WORKGROUP, which
    # maps to no account.
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive
    Register-ScheduledTask -TaskName $task -Action $action -Principal $principal -Force | Out-Null
    $t0 = Get-Date
    Start-ScheduledTask -TaskName $task
    $gpid = $null
    while (-not (Test-Path -LiteralPath $doneFile)) {
        if (-not $gpid -and (Test-Path -LiteralPath $pidFile)) { $gpid = [int](Get-Content -LiteralPath $pidFile) }
        $el = ((Get-Date) - $t0).TotalSeconds
        if (-not $gpid -and $el -gt 60) { Write-Host "bench: FAIL the game did not start in 60 s (is the user logged on?)"; exit 1 }
        # The backstop: the game should have quit itself at the limit.
        if ($p.timeout -and $el -gt $p.timeout + 30) { break }
        Start-Sleep -Seconds 1
    }
    $killed = $false
    if (-not (Test-Path -LiteralPath $doneFile)) {
        if ($gpid) { & taskkill /F /T /PID $gpid | Out-Null }
        $killed = $true
        for ($i = 0; $i -lt 10 -and -not (Test-Path -LiteralPath $doneFile); $i++) { Start-Sleep -Seconds 1 }
    }
    $elapsed = [int]((Get-Date) - $t0).TotalSeconds
    $done = if (Test-Path -LiteralPath $doneFile) { (Get-Content -LiteralPath $doneFile) -split ' ', 3 } else { @('?', $elapsed) }
    if ($done[0] -eq 'launch-failed') { Write-Host "bench: FAIL the task could not start the game: $($done[2])"; exit 1 }
    # check_run_end's words, as a Deck run gives them: 124 for a run that
    # reached its limit (the game's own quit, or the backstop kill).
    $code = $done[0]
    if ($killed) { $code = 124 }
    elseif ($p.timeout -and $code -eq '0' -and [int]$done[1] -ge $p.timeout) { $code = 124 }
    # ASCII: PowerShell 5.1's UTF8 writes a BOM, which int() on the Mac rejects.
    Set-Content -Encoding ASCII -LiteralPath "$log\exit-code" "$code"

    $stdio = "$log\game-stdio.log"
    $flips = '? (no game-stdio.log)'
    if (Test-Path -LiteralPath $stdio) {
        $n = @(Select-String -LiteralPath $stdio -Pattern '^\[D3D11\] flip').Count
        if ($n) { $flips = "$n" }
        else {
            $last = Select-String -LiteralPath $stdio -Pattern '\[d3d11\] present #(\d+):' | ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Measure-Object -Maximum
            $flips = if ($last.Count) { "$($last.Maximum) (at least: no flip trace, the last [d3d11] present #N line; logged every 300)" } else { '0' }
        }
    }
    $load1 = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    # Load is a snapshot, not PSI: a busy start (50%+ before the game ran)
    # is what makes a slow run inconclusive here.
    $busy = if ($load0 -ge 50) { "yes (cpu load $load0% at start)" } else { 'no' }
    $more = @(
        "flips:  $flips"
        "load:   $load0% at start, $load1% at end ($env:NUMBER_OF_PROCESSORS cpus)"
        "busy:   $busy"
        "elapsed: ${elapsed}s, game exit $($done[0])$(if ($killed) { ', killed by the backstop past the limit' })"
    )
    Add-Content -LiteralPath "$log\run-info.txt" $more

    # Anything of this run still alive (the game's process tree)?
    Start-Sleep -Seconds 2
    $left = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    if ($left.Count) {
        $s = @("survived the run (exit $code):") + ($left | ForEach-Object { "$($_.Id) $($_.Path)" })
        $left | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $still = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        if ($still.Count) { $s += 'still alive after Stop-Process:'; $s += ($still | ForEach-Object { "$($_.Id) $($_.Path)" }) }
        else { $s += 'killed: none left after Stop-Process' }
        Set-Content -LiteralPath "$log\survived" $s
        $s | ForEach-Object { Write-Host "!!! bench: $_" }
    }
    Write-Host "exit code $code; logs in $log"
    exit 0
} finally {
    # A run that failed to start leaves no task behind to start it late.
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    if ($lockStream -and "$(Get-Content -Raw -LiteralPath $p.lock -ErrorAction SilentlyContinue)".Trim() -eq $mine) {
        Remove-Item -LiteralPath $p.lock
    }
}
