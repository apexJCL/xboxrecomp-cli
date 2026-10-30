"""The Godot side: the pinned editor, the project the stages go in, and
the headless import that checks them.

Godot is pinned like the other toolchains (pins.py's UMU_LAUNCHER): the
official release zip per host with its sha256 and size, moved by a
reviewed edit. It installs into the game's third_party/godot-<version>/
(gitignored, beside llvm-mingw); $XBR_GODOT names a binary to use
instead, which must report the pinned version. Nothing here exports a
.pck or runs the project: an export would package game data.
"""

import os
import shutil
import subprocess

from .. import fetch, host
from ..host import CliError

VERSION = "4.7.2"
VERSION_PREFIX = "4.7.2.stable"
URL = "https://github.com/godotengine/godot/releases/download/%s-stable/" % VERSION
# host key -> (asset, sha256, size, binary inside the zip)
ASSETS = {
    "macos-x86_64": (
        "Godot_v4.7.2-stable_macos.universal.zip",
        "c58a24e31d720be9d62f60cb5627c4e695fb72f21b0cfe1bc9ccaa9a3b3ba63e",
        170622178,
        "Godot.app/Contents/MacOS/Godot",
    ),
    "linux-x86_64": (
        "Godot_v4.7.2-stable_linux.x86_64.zip",
        "cadd3204e728a35d3f13adb7fd0d7902636b79f6b95c40c265eb73b6c35329e4",
        77860424,
        "Godot_v4.7.2-stable_linux.x86_64",
    ),
    "linux-aarch64": (
        "Godot_v4.7.2-stable_linux.arm64.zip",
        "5dd0d86405cf7e8adf79fb6377b38ba682a2846cb378ffe5364f38c01ad29b9d",
        77008699,
        "Godot_v4.7.2-stable_linux.arm64",
    ),
    "windows-x86_64": (
        "Godot_v4.7.2-stable_win64.exe.zip",
        "731980f9608d61333e5baf54a2ef17210acc7a538446c0cb9969f002aca1e953",
        86013866,
        "Godot_v4.7.2-stable_win64_console.exe",
    ),
}
ASSETS["macos-aarch64"] = ASSETS["macos-x86_64"]  # one universal build
ENV = "XBR_GODOT"


def host_key(os_name=None, arch=None):
    return "%s-%s" % (os_name or host.host_os(), arch or host.host_arch())


def install_dir(third_party):
    return os.path.join(third_party, "godot-%s" % VERSION)


def version_of(binary):
    try:
        r = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError as e:
        raise CliError("%s: %s" % (binary, e)) from e
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def ensure(third_party, environ=None):
    """The Godot binary: $XBR_GODOT, else the pinned build under
    third_party/, fetched and sha256-checked when missing."""
    environ = os.environ if environ is None else environ
    if environ.get(ENV):
        binary = os.path.abspath(environ[ENV])
    else:
        key = host_key()
        if key not in ASSETS:
            raise CliError("no pinned Godot %s for %s; set %s" % (VERSION, key, ENV))
        asset, sha, size, inner = ASSETS[key]
        dest = install_dir(third_party)
        binary = os.path.join(dest, inner)
        if not os.path.isfile(binary):
            os.makedirs(third_party, exist_ok=True)
            arc = os.path.join(third_party, asset)
            fetch.download(URL + asset, arc, sha, size)
            part = dest + ".partial"
            shutil.rmtree(part, ignore_errors=True)
            fetch.extract(arc, part, strip=0)
            shutil.rmtree(dest, ignore_errors=True)
            os.rename(part, dest)
            os.remove(arc)
    if not os.path.isfile(binary):
        raise CliError("%s: no Godot binary there" % binary)
    v = version_of(binary)
    if not v.startswith(VERSION_PREFIX):
        raise CliError("%s reports %r; xbr extract needs Godot %s" % (binary, v, VERSION))
    return binary


PROJECT = """\
; Made by `xbr extract` from the owner's own dump: game data, never share it.
config_version=5

[application]
config/name="{title}"
run/main_scene="res://{main}"

[audio]
driver/driver="Dummy"

[rendering]
renderer/rendering_method="forward_plus"
"""

# The viewer every stage scene runs: frames the stage's bounds with a
# camera, a sun and a flat sky, since the game's lighting and fog live in
# its code, not in the data. `-- --shot PATH` saves one frame and quits.
VIEW_GD = """\
extends Node3D

var frames := 0
var shot := ""


func _ready():
	var args := OS.get_cmdline_user_args()
	var i := args.find("--shot")
	if i >= 0 and i + 1 < args.size():
		shot = args[i + 1]
	var aabb := AABB()
	var first := true
	for mi in find_children("*", "MeshInstance3D", true, false):
		var b: AABB = mi.global_transform * mi.get_aabb()
		aabb = b if first else aabb.merge(b)
		first = false
	var env := WorldEnvironment.new()
	var e := Environment.new()
	e.background_mode = Environment.BG_COLOR
	e.background_color = Color(0.35, 0.45, 0.6)
	e.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	e.ambient_light_color = Color(1, 1, 1)
	e.ambient_light_energy = 0.6
	env.environment = e
	add_child(env)
	var sun := DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(-55, 35, 0)
	add_child(sun)
	var cam := Camera3D.new()
	var c := aabb.get_center()
	var r := maxf(aabb.size.length() * 0.5, 1.0)
	cam.far = r * 8.0
	add_child(cam)
	cam.position = c + Vector3(-0.55, 0.75, -0.55).normalized() * r * 1.25
	cam.look_at(c, Vector3.UP)
	cam.current = true


func _process(_delta):
	frames += 1
	if shot != "" and frames == 30:
		get_viewport().get_texture().get_image().save_png(shot)
		get_tree().quit(0)
"""

SCENE = """\
[gd_scene load_steps=3 format=3]

[ext_resource type="Script" path="res://view.gd" id="1"]
[ext_resource type="PackedScene" path="res://stages/{name}/{name}.gltf" id="2"]

[node name="{name}" type="Node3D"]
script = ExtResource("1")

[node name="stage" parent="." instance=ExtResource("2")]
"""

GITIGNORE = "# Game data made by xbr extract: never commit it.\n*\n"


def write_project(out, title, stages):
    """project.godot, view.gd and one <stage>.tscn per stage; the glTF
    files are under stages/<name>/."""
    os.makedirs(out, exist_ok=True)
    main = (stages[0] + ".tscn") if stages else ""
    files = {
        "project.godot": PROJECT.format(title=title.replace('"', "'"), main=main),
        "view.gd": VIEW_GD,
        ".gitignore": GITIGNORE,
    }
    for name in stages:
        files[name + ".tscn"] = SCENE.format(name=name)
    for rel, text in files.items():
        with open(os.path.join(out, rel), "w", newline="\n") as f:
            f.write(text)


def import_project(binary, out, log_path):
    """Imports the project headless (no window, no sound); CliError when
    Godot fails or logs an error. Returns the log's lines."""
    cmd = [binary, "--headless", "--audio-driver", "Dummy", "--path", out, "--import"]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    log = r.stdout + r.stderr
    with open(log_path, "w") as f:
        f.write(log)
    errors = [ln for ln in log.splitlines() if ln.lstrip().startswith(("ERROR", "SCRIPT ERROR"))]
    if r.returncode != 0 or errors:
        raise CliError(
            "Godot import failed (exit %d, %d errors; log %s)%s"
            % (
                r.returncode,
                len(errors),
                log_path,
                ("\n  " + "\n  ".join(errors[:5])) if errors else "",
            )
        )
    return log.splitlines()
