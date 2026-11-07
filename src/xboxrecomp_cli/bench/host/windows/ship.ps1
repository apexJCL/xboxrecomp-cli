# bench/host/windows/ship.ps1: `<game> bench` on a Windows host
# (BENCH_HOST_OS=windows), before it sends a locally built exe by scp.
# Params (JSON): run_dir; exact (the exe's directory under it, which ends up
# holding exactly the listed files there, flat ones and shaders/'s); exe;
# files ([dir, name, sha256], name relative to dir with / separators).
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
        # Only what the pruning below keeps exact: the flat files and
        # shaders/. Any other directory (a save, a dump) would stay unlisted.
        Get-ChildItem -File -LiteralPath $seed | Copy-Item -Destination $exact
        $s = Join-Path $seed 'shaders'
        if (Test-Path -LiteralPath $s) { Copy-Item -Recurse -LiteralPath $s -Destination $exact }
        Write-Host "ship: seeded from $(Split-Path (Split-Path $seed))"
    }
}
$listed = @{}
foreach ($f in $p.files) { $listed["$($f[0])/$($f[1])"] = $f[2] }
# What an earlier build had and this one lacks (a PDB, a DLL) goes, so the
# directory holds exactly this build's files.
$held = @(Get-ChildItem -File -LiteralPath $exact)
$held += @(Get-ChildItem -File -Recurse -LiteralPath (Join-Path $exact 'shaders') -ErrorAction SilentlyContinue)
$root = (Get-Item -LiteralPath $exact).FullName.TrimEnd('\')
foreach ($f in $held) {
    $rel = $f.FullName.Substring($root.Length + 1).Replace('\', '/')
    if (-not $listed.ContainsKey("$($p.exact)/$rel")) {
        Remove-Item -LiteralPath $f.FullName
        Write-Host "ship: removed $($p.exact)/$rel"
    }
}
foreach ($f in $p.files) {
    # A name may hold a directory (shaders/crt/...): scp makes none.
    $t = Join-Path (Join-Path $run $f[0]) $f[1]
    New-Item -ItemType Directory -Force -Path (Split-Path $t) | Out-Null
    if (-not (Test-Path -LiteralPath $t) -or (Get-FileHash -LiteralPath $t).Hash.ToLower() -ne $f[2]) {
        Write-Output "need $($f[0])/$($f[1])"
    }
}
