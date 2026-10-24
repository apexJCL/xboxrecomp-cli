# bench/host/windows/launch.ps1: what the bench's one-shot scheduled task
# runs in the logged-on desktop session (an ssh session has no desktop, so
# a game started from it gets no window). Starts the game with exactly the
# bench's RECOMP_* env, writes its pid, waits, and writes `<exit code>
# <seconds>` to the done file. Params (JSON, left for the controller):
# exe, cwd, args, env ([key, value]), pid_file, done_file.
param([Parameter(Mandatory)][string]$Params)
$p = Get-Content -Raw -Encoding UTF8 -LiteralPath $Params | ConvertFrom-Json
try {
    # The task inherits the user's environment: a RECOMP_* left there must
    # not reach the run.
    Get-ChildItem env: | Where-Object { $_.Name -like 'RECOMP_*' } | ForEach-Object { Remove-Item "env:$($_.Name)" }
    foreach ($kv in $p.env) { Set-Item "env:$($kv[0])" $kv[1] }
    $t0 = Get-Date
    $a = @{ FilePath = $p.exe; WorkingDirectory = $p.cwd; PassThru = $true }
    if ($p.args.Count) { $a.ArgumentList = $p.args }
    $proc = Start-Process @a
    # The handle keeps the exit code readable after the process ends.
    $null = $proc.Handle
    Set-Content -LiteralPath $p.pid_file "$($proc.Id)"
    $proc.WaitForExit()
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    Set-Content -LiteralPath $p.done_file "$($proc.ExitCode) $secs"
} catch {
    Set-Content -LiteralPath $p.done_file "launch-failed 0 $_"
}
