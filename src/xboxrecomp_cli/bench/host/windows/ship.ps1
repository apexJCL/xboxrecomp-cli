# bench/host/windows/ship.ps1: `<game> bench` on a Windows host
# (BENCH_HOST_OS=windows), before it sends a locally built exe by scp.
# Params (JSON): run_dir; exact (the exe's directory under it, which ends up
# holding exactly the listed files there); exe; files ([dir, name, sha256]).
# Prints `need <dir>/<name>` for each file the host lacks or holds other
# bytes of; the controller sends only those.
param([Parameter(Mandatory)][string]$Params)
$ErrorActionPreference = 'Stop'
$p = Get-Content -Raw -Encoding UTF8 -LiteralPath $Params | ConvertFrom-Json
Remove-Item -LiteralPath $Params
$run = $p.run_dir
$exact = Join-Path $run $p.exact
New-Item -ItemType Directory -Force -Path $exact, (Join-Path $run 'bench-logs') | Out-Null
# Its time is when it was last shipped to: benchdb clean keeps a run
# directory shipped to recently (D11 guard 5), whatever its files say.
(Get-Item -LiteralPath $run).LastWriteTime = Get-Date
# A new run directory starts as a copy of the newest other one's exe
# directory: scp has no deltas, so the runtime files that did not change
# (DLLs, data) are then not sent again.
if (-not (Test-Path -LiteralPath (Join-Path $exact $p.exe))) {
    $seed = Get-ChildItem -Directory -LiteralPath (Split-Path $run) -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne (Get-Item -LiteralPath $run).FullName } |
        ForEach-Object { Join-Path $_.FullName $p.exact } |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ $p.exe) } |
        Sort-Object { (Get-Item -LiteralPath (Join-Path $_ $p.exe)).LastWriteTime } |
        Select-Object -Last 1
    if ($seed) {
        Get-ChildItem -File -LiteralPath $seed | Copy-Item -Destination $exact
        Write-Host "ship: seeded from $(Split-Path (Split-Path $seed))"
    }
}
$listed = @{}
foreach ($f in $p.files) { $listed["$($f[0])/$($f[1])"] = $f[2] }
# What an earlier build had and this one lacks (a PDB, a DLL) goes, so the
# directory holds exactly this build's files.
foreach ($f in Get-ChildItem -File -LiteralPath $exact) {
    if (-not $listed.ContainsKey("$($p.exact)/$($f.Name)")) {
        Remove-Item -LiteralPath $f.FullName
        Write-Host "ship: removed $($p.exact)/$($f.Name)"
    }
}
foreach ($f in $p.files) {
    $d = Join-Path $run $f[0]
    New-Item -ItemType Directory -Force -Path $d | Out-Null
    $t = Join-Path $d $f[1]
    if (-not (Test-Path -LiteralPath $t) -or (Get-FileHash -LiteralPath $t).Hash.ToLower() -ne $f[2]) {
        Write-Output "need $($f[0])/$($f[1])"
    }
}
