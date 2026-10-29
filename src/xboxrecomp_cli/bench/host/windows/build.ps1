# bench/host/windows/build.ps1: a native Windows build host
# (BENCH_BUILD_HOST): the controller sends git bundles of the game, the
# toolkit and the CLI, and gen/ as a tarball when it changed; this checks
# them out under root and runs the CLI there with llvm-mingw, CMake and
# Ninja from the game's own `setup` (pinned, sha256-checked, user space).
# Params (JSON): phase (state | build), root (<BENCH_BUILD_DIR>/<game>),
# call (this call's id: its upload dir .in/<call>, its output dir
# .out/<call>), repos ([name, bundle or '', commit]), ref (the bundles' one
# ref), gen ({tar, sha256, digest, rel, key, build}: build is the windows
# build dir, whose gen/ objects go when a tarball comes), slug, args (the
# CLI's argv), exe_rel (the exe under the game tree), pull (paths of the
# game tree, a glob in the last part, copied out after the command),
# mingw (llvm-mingw's tag), game_files ({path, rel}: the host's own copy,
# linked in for this call only), lock, lock_held, owner.
# state prints `head <name> <sha>` per repo and `gen <digest>`; build
# prints the CLI's output, then `@xbr-file <name> <sha256>` per runtime
# file beside the exe (ship.runtime_files' set) and `@xbr-pulled <name>`
# per pulled path, all copied into .out/<call> under the lock, and exits
# with the CLI's code. `@xbr-status lock-timeout` (exit 75) and
# `@xbr-status gen-moved` (exit 76: gen/ here is not the digest the
# controller probed, and it sent none) are this script's own.
param([Parameter(Mandatory)][string]$Params)
$ErrorActionPreference = 'Stop'
$p = Get-Content -Raw -Encoding UTF8 -LiteralPath $Params | ConvertFrom-Json
Remove-Item -LiteralPath $Params
$root = $p.root
$genMark = Join-Path $root 'gen-digest.txt'
$game = Join-Path $root 'game'

# gen_state's digest (buildhost.py) over what this tree holds: sha256 of
# the `sha256  name` listing of gen/ sorted by name, dotfiles left out,
# then that hex digest's bytes followed by gen.key.json's.
function Get-GenDigest {
    $dir = Join-Path $game $p.gen.rel
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return '' }
    [string[]]$names = @(Get-ChildItem -Force -File -LiteralPath $dir |
        Where-Object { -not $_.Name.StartsWith('.') } | ForEach-Object { $_.Name })
    [Array]::Sort($names, [StringComparer]::Ordinal)
    $sb = New-Object Text.StringBuilder
    foreach ($n in $names) {
        $h = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $dir $n)).Hash.ToLower()
        [void]$sb.Append("$h  $n`n")
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    $gd = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString())) | ForEach-Object { $_.ToString('x2') })
    [byte[]]$all = [Text.Encoding]::UTF8.GetBytes($gd)
    $key = Join-Path $game $p.gen.key
    if (Test-Path -LiteralPath $key -PathType Leaf) { [byte[]]$all = $all + [IO.File]::ReadAllBytes($key) }
    return -join ($sha.ComputeHash($all) | ForEach-Object { $_.ToString('x2') })
}

function Head([string]$dir) {
    if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) { return '' }
    $h = & git -C $dir rev-parse -q --verify 'refs/xbr/head^{commit}' 2>$null
    if ($LASTEXITCODE) { return '' }
    return "$h".Trim()
}

if ($p.phase -eq 'state') {
    foreach ($r in $p.repos) { Write-Output "head $($r[0]) $(Head (Join-Path $root $r[0]))" }
    $g = if (Test-Path -LiteralPath $genMark) { "$(Get-Content -Raw -LiteralPath $genMark)".Trim() } else { '' }
    Write-Output "gen $g"
    exit 0
}

$in = Join-Path (Join-Path $root '.in') $p.call

# One job at a time on this PC: builds take the runs' lock file
# (run_game.ps1), with the same takeover of a lock whose holder is gone.
$lockStream = $null
$me = Get-Process -Id $PID
$mine = "$($p.owner) pid=$PID start=$($me.StartTime.ToUniversalTime().Ticks)"
function Test-StaleLock([string]$text) {
    if ($text -notmatch '^bench .* pid=(\d+) start=(\d+)\s*$') { return $false }
    $h = Get-Process -Id ([int]$Matches[1]) -ErrorAction SilentlyContinue
    return -not $h -or "$($h.StartTime.ToUniversalTime().Ticks)" -ne $Matches[2]
}
if ($p.lock_held) {
    Write-Host "build: $($p.lock) is held by the caller (BENCH_LOCK_HELD=1, this host's own lock)"
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
            $took = $false
            if ($held -and (Test-StaleLock $held)) {
                try {
                    $s = [IO.File]::Open($p.lock, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
                    try {
                        $buf = New-Object byte[] $s.Length
                        $n = $s.Read($buf, 0, $buf.Length)
                        if ([Text.Encoding]::UTF8.GetString($buf, 0, $n).Trim() -eq $held) {
                            $bytes = [Text.Encoding]::UTF8.GetBytes("$mine`n")
                            $s.SetLength(0)
                            $s.Write($bytes, 0, $bytes.Length)
                            $lockStream = $s
                            $took = $true
                        }
                    } finally { $s.Close() }
                } catch {
                }
                if ($took) {
                    Write-Host "build: took over $($p.lock): its holder is gone ($held)"
                    break
                }
            }
            if (-not $said) {
                Write-Host "build: waiting for $($p.lock), held by: $held"
                $said = $true
            }
            if (((Get-Date) - $t0).TotalSeconds -ge 3600) {
                Write-Host "build: FAIL $($p.lock) was held for an hour by: $held"
                Write-Output '@xbr-status lock-timeout'
                # Before the try below: its finally never runs for this exit,
                # and the uploads (a gen tarball can be large) would sit
                # until the one-day sweep.
                if (Test-Path -LiteralPath $in) { Remove-Item -Recurse -Force -LiteralPath $in }
                exit 75
            }
            Start-Sleep -Seconds 5
        }
    }
    if ($said) { Write-Host "build: got $($p.lock) after $([int]((Get-Date) - $t0).TotalSeconds)s" }
}

$out = Join-Path (Join-Path $root '.out') $p.call
$gf = $null
try {
    # What a controller that died mid-call left behind: only this script's
    # own dirs, and never a link (Copy-Item copies a junction's content).
    foreach ($d in @((Join-Path $root '.in'), (Join-Path $root '.out'))) {
        Get-ChildItem -Directory -LiteralPath $d -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne $p.call -and $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
            ForEach-Object { Remove-Item -Recurse -Force -LiteralPath $_.FullName }
    }
    foreach ($r in $p.repos) {
        $dir = Join-Path $root $r[0]
        if (-not (Test-Path -LiteralPath (Join-Path $dir '.git'))) {
            New-Item -ItemType Directory -Force -Path $dir | Out-Null
            & git -C $dir init -q
            if ($LASTEXITCODE) { throw "git init $dir failed" }
            # The bytes the Mac compiles: Git for Windows' default autocrlf
            # would check sources out with CRLF.
            & git -C $dir config core.autocrlf false
            & git -C $dir config core.longpaths true
        }
        if ($r[1]) {
            $bundle = Join-Path $in $r[1]
            & git -C $dir fetch -q --no-tags $bundle "+$($p.ref):refs/xbr/in"
            if ($LASTEXITCODE) { throw "git fetch $bundle failed" }
        }
        & git -C $dir update-ref refs/xbr/head $r[2]
        if ($LASTEXITCODE) { throw "$($r[0]): no commit $($r[2]) (bundle missing?)" }
        & git -c advice.detachedHead=false -C $dir checkout -q -f --detach $r[2]
        if ($LASTEXITCODE) { throw "$($r[0]): checkout $($r[2]) failed" }
        Write-Host "build: $($r[0]) at $($r[2].Substring(0, 12))"
    }
    if ($p.gen.tar) {
        $tar = Join-Path $in $p.gen.tar
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $tar).Hash.ToLower()
        if ($got -ne $p.gen.sha256) { throw "$($p.gen.tar) arrived as sha256 $got, not $($p.gen.sha256)" }
        $gen = Join-Path $game $p.gen.rel
        if (Test-Path -LiteralPath $gen) { Remove-Item -Recurse -Force -LiteralPath $gen }
        Remove-Item -Force -LiteralPath $genMark -ErrorAction SilentlyContinue
        # The key sits beside gen/, not in it, and the tarball carries one
        # only when the controller has one: an old key left here would
        # enter the digest below and fail every call until a key came.
        Remove-Item -Force -LiteralPath (Join-Path $game $p.gen.key) -ErrorAction SilentlyContinue
        # -m: the files get this host's time. The tarball carries the
        # controller's mtimes, which can be older than objects another
        # controller's gen/ left here, and ninja would then keep those.
        & tar -m -xzf $tar -C $game
        if ($LASTEXITCODE) { throw "unpacking $($p.gen.tar) failed" }
        # And the objects compiled from the old gen/ go too, whatever the
        # clocks say.
        $objs = Join-Path (Join-Path $game $p.gen.build) 'CMakeFiles'
        Get-ChildItem -Directory -Filter '*.dir' -LiteralPath $objs -ErrorAction SilentlyContinue | ForEach-Object {
            $o = Join-Path $_.FullName $p.gen.rel
            if (Test-Path -LiteralPath $o) { Remove-Item -Recurse -Force -LiteralPath $o }
        }
        # A recomp stopped on the host leaves its marker beside gen/, and
        # the CLI then refuses the complete gen/ that just came.
        Remove-Item -Force -LiteralPath (Join-Path $game $p.gen.marker) -ErrorAction SilentlyContinue
    }
    # Under the lock, over the files themselves: another controller's call
    # may have changed gen/ since this one's state probe.
    $have = Get-GenDigest
    if ($have -ne $p.gen.digest) {
        Remove-Item -Force -LiteralPath $genMark -ErrorAction SilentlyContinue
        if ($p.gen.tar) { throw "gen/ unpacked as $have, not the $($p.gen.digest) sent" }
        Write-Host "build: gen/ here is $(if ($have) { $have.Substring(0, 12) } else { 'missing' }), not $($p.gen.digest.Substring(0, 12)); the controller sends it"
        Write-Output '@xbr-status gen-moved'
        exit 76
    }
    Set-Content -LiteralPath $genMark $have
    if ($p.gen.tar) { Write-Host "build: gen/ $($have.Substring(0, 12)) unpacked" }
    # A junction, not a copy: the game files stay where the PC keeps them,
    # and git ignores the link. Only for this call: a recursive delete of
    # the tree in Windows PowerShell would follow it into the game files.
    if ($p.game_files.path) {
        $gf = Join-Path $game $p.game_files.rel
        $attr = $null
        try { $attr = [IO.File]::GetAttributes($gf) } catch { }
        if ($null -ne $attr) {
            if (-not ($attr -band [IO.FileAttributes]::ReparsePoint)) {
                $gf = $null
                throw "$($p.game_files.rel) is a real folder in $game; move it out of the build tree"
            }
            # A link an earlier call left (or one to a gone target): never
            # followed, only the link itself goes.
            [IO.Directory]::Delete($gf)
        }
        New-Item -ItemType Junction -Path $gf -Target $p.game_files.path | Out-Null
        Write-Host "build: $($p.game_files.rel) -> $($p.game_files.path) for this call"
    }

    $env:XBOXRECOMP_DIR = Join-Path $root 'xboxrecomp'
    $env:XBOXRECOMP_CLI_DIR = Join-Path $root 'xboxrecomp-cli'
    Remove-Item Env:VIRTUAL_ENV, Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue
    $xbr = @('run', '--quiet', '--project', $env:XBOXRECOMP_CLI_DIR, '--locked', '--no-dev',
        'xbr', '--game', $game, '--prog', $p.slug)
    # setup fetches only what its pins lack. Its exit status is doctor's
    # packaging verdict, which fails here for want of game files (the build
    # host has none and needs none), so success is the toolchain in place.
    & uv @xbr setup --no-toolkit
    $setupRc = $LASTEXITCODE
    $tools = @((Join-Path $game '.venv\Scripts\cmake.exe'), (Join-Path $game '.venv\Scripts\ninja.exe'))
    $missing = @($tools | Where-Object { -not (Test-Path -LiteralPath $_) })
    if (-not (Get-ChildItem -Directory -Path (Join-Path $game 'third_party') -Filter "llvm-mingw-$($p.mingw)-*" -ErrorAction SilentlyContinue)) {
        $missing += "third_party\llvm-mingw-$($p.mingw)-*"
    }
    if ($missing) {
        Write-Host "build: FAIL $($p.slug) setup (exit $setupRc) left no $($missing -join ', ')"
        exit 1
    }
    # Only a build leaves gen/ as it was sent; any other command (package)
    # may regenerate it, so the next run sends it again.
    if (-not $p.exe_rel) { Remove-Item -Force -LiteralPath $genMark -ErrorAction SilentlyContinue }
    & uv @xbr @($p.args)
    $code = $LASTEXITCODE
    # Copied out before the lock goes, so the next call cannot relink the
    # exe (or rewrite dist/) under the controller's pull.
    if ($code -eq 0 -and ($p.exe_rel -or $p.pull)) {
        New-Item -ItemType Directory -Force -Path $out | Out-Null
    }
    if ($code -eq 0 -and $p.exe_rel) {
        $exe = Join-Path $game $p.exe_rel
        $dir = Split-Path $exe
        $files = @(Get-Item -LiteralPath $exe)
        foreach ($x in @([IO.Path]::ChangeExtension($exe, '.pdb'), (Join-Path $dir 'config.default.toml'))) {
            if (Test-Path -LiteralPath $x) { $files += Get-Item -LiteralPath $x }
        }
        $files += @(Get-ChildItem -File -LiteralPath $dir -Filter '*.dll' | Sort-Object Name)
        foreach ($f in $files) {
            Copy-Item -LiteralPath $f.FullName -Destination $out
            Write-Output "@xbr-file $($f.Name) $((Get-FileHash -LiteralPath (Join-Path $out $f.Name)).Hash.ToLower())"
        }
    }
    if ($code -eq 0 -and $p.pull) {
        $seen = @{}
        # The game files (linked in for package) never leave this PC: not
        # through a glob that matches the link, a different case or a
        # folder that holds it. NTFS compares paths without case.
        $gfFull = [IO.Path]::GetFullPath((Join-Path $game $p.game_files.rel)).TrimEnd('\')
        $ic = [StringComparison]::OrdinalIgnoreCase
        foreach ($rel in @($p.pull)) {
            $items = @(Get-Item -Path (Join-Path $game $rel) -ErrorAction SilentlyContinue)
            if (-not $items) { Write-Host "build: warning: --pull $rel matched nothing" }
            foreach ($it in $items) {
                $f = [IO.Path]::GetFullPath($it.FullName).TrimEnd('\')
                if ($f.Equals($gfFull, $ic) -or $f.StartsWith("$gfFull\", $ic) -or $gfFull.StartsWith("$f\", $ic)) {
                    Write-Host "build: --pull $rel`: skipped $($it.Name), the game files or a folder holding them"
                    continue
                }
                if ($seen.ContainsKey($it.Name)) { throw "--pull: two paths named $($it.Name)" }
                $seen[$it.Name] = $true
                Copy-Item -Recurse -LiteralPath $it.FullName -Destination $out
                Write-Output "@xbr-pulled $($it.Name)"
            }
        }
    }
    exit $code
} finally {
    if ($gf) {
        try { [IO.Directory]::Delete($gf) } catch { Write-Host "build: warning: could not remove the link $gf" }
    }
    if (Test-Path -LiteralPath $in) { Remove-Item -Recurse -Force -LiteralPath $in }
    if ($lockStream -and "$(Get-Content -Raw -LiteralPath $p.lock -ErrorAction SilentlyContinue)".Trim() -eq $mine) {
        Remove-Item -LiteralPath $p.lock
    }
}
