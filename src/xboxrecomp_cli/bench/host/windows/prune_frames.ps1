# bench/host/windows/prune_frames.ps1: after a golden check passed, the
# run's frames/ on a Windows host goes (the Mac kept what it read).
# Params (JSON): run_dir; stamps (the runs whose frames/ go).
param([Parameter(Mandatory)][string]$Params)
$ErrorActionPreference = 'Stop'
$p = Get-Content -Raw -Encoding UTF8 -LiteralPath $Params | ConvertFrom-Json
Remove-Item -LiteralPath $Params
$rc = 0
foreach ($s in $p.stamps) {
    # A stamp is digits and one dash (golden.STAMP_RE): never a path.
    if ($s -notmatch '^\d{8}-\d{6}$') { Write-Host "prune: not a stamp: $s"; $rc = 1; continue }
    $f = Join-Path $p.run_dir "bench-logs\$s\frames"
    try { Remove-Item -Recurse -Force -LiteralPath $f -ErrorAction Stop }
    catch [System.Management.Automation.ItemNotFoundException] { }
    catch { Write-Host "prune: ${f}: $_"; $rc = 1 }
}
exit $rc
