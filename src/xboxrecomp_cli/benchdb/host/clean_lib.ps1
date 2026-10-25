#@ benchdb/host/clean_lib.ps1: the functions clean_list.ps1 and
#@ clean_apply.ps1 share, sent before either. The #@ lines this file starts
#@ with are this note and are not sent.
# A walk by hand, one directory at a time: Windows PowerShell 5.1's
# Get-ChildItem -Recurse can descend into a junction while it only filters
# what it outputs, and Remove-Item -Recurse follows one. A reparse point
# (junction, directory or file symlink, mount point) is never entered.
$ErrorActionPreference = 'Stop'
$RP = [IO.FileAttributes]::ReparsePoint
$DIR = [IO.FileAttributes]::Directory
$RO = [IO.FileAttributes]::ReadOnly
function Out-Line([string[]]$f) { [Console]::Out.Write(($f -join "`t") + "`n") }
function Get-Unix([string]$p, $a) {
    $t = if (($a -band $DIR) -ne 0) { [IO.Directory]::GetLastWriteTimeUtc($p) } else { [IO.File]::GetLastWriteTimeUtc($p) }
    return [DateTimeOffset]::new($t).ToUnixTimeSeconds()
}
function Measure-Tree([string]$top) {
    $newest = [int64]0
    $bytes = [int64]0
    $stack = New-Object System.Collections.Stack
    $stack.Push($top)
    while ($stack.Count) {
        $p = $stack.Pop()
        $a = [IO.File]::GetAttributes($p)
        $s = Get-Unix $p $a
        if ($s -gt $newest) { $newest = $s }
        if (($a -band $RP) -ne 0) { continue }
        if (($a -band $DIR) -ne 0) {
            foreach ($e in [IO.Directory]::EnumerateFileSystemEntries($p)) { $stack.Push($e) }
        } else {
            $bytes += (New-Object IO.FileInfo $p).Length
        }
    }
    return @($newest, $bytes)
}
# The resolved path of p: the full path of its parent plus its name, and
# the first reparse point among the parent and its ancestors below root
# ('' when none). GetFullPath only normalises the text, so the reparse
# test is what stands in for POSIX realpath.
function Resolve-Leaf([string]$p, [string]$root) {
    $parent = [IO.Path]::GetFullPath([IO.Path]::GetDirectoryName($p))
    $res = [IO.Path]::Combine($parent, [IO.Path]::GetFileName($p))
    $via = ''
    $d = $parent
    while ($d -and $d.Length -gt $root.TrimEnd('\').Length) {
        if ((Test-Path -LiteralPath $d) -and (([IO.File]::GetAttributes($d) -band $RP) -ne 0)) { $via = $d; break }
        $d = [IO.Path]::GetDirectoryName($d)
    }
    if (-not $via -and (Test-Path -LiteralPath $root) -and (([IO.File]::GetAttributes($root) -band $RP) -ne 0)) { $via = $root }
    return @($res, $via)
}
function Test-Under([string]$p, [string]$base) {
    $b = $base.TrimEnd('\')
    return $p.Equals($b, 'OrdinalIgnoreCase') -or $p.StartsWith($b + '\', 'OrdinalIgnoreCase')
}
