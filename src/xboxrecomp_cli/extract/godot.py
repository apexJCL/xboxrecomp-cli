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
import re
import shutil
import subprocess

from .. import fetch, host
from ..host import CliError
from . import collision

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

# What glTF cannot say, applied to the imported stage when it enters the
# tree (and in the editor, as a tool script): a material the writer marked
# with the extra "xbr_blend": "add" (Ninja blend 0x21, source alpha plus
# one) blends additively. Godot's importer keeps a material's glTF extras
# as its "extras" metadata. The viewer is a separate script, so a scene
# that swaps it keeps this. It only looks at the mesh's own surface
# materials: a material_override or surface_override_material, or a glTF
# root type set to plain Node (this script extends Node3D), is not handled.
# The project ships with Godot's default import settings, which set none of
# them; whoever changes them in the editor owns the result.
STAGE_GD = """\
@tool
extends Node3D


func _ready():
	for mi in find_children("*", "MeshInstance3D", true, false):
		var mesh: Mesh = mi.mesh
		if mesh == null:
			continue
		for i in mesh.get_surface_count():
			var mat := mesh.surface_get_material(i) as BaseMaterial3D
			if mat == null or not mat.has_meta("extras"):
				continue
			var x = mat.get_meta("extras")
			if x is Dictionary and x.get("xbr_blend", "") == "add":
				mat.blend_mode = BaseMaterial3D.BLEND_MODE_ADD
"""

SCENE = """\
[gd_scene load_steps=4 format=3]

[ext_resource type="Script" path="res://view.gd" id="1"]
[ext_resource type="PackedScene" path="res://stages/{name}/{name}.gltf" id="2"]
[ext_resource type="Script" path="res://stage.gd" id="3"]

[node name="{name}" type="Node3D"]
script = ExtResource("1")

[node name="stage" parent="." instance=ExtResource("2")]
script = ExtResource("3")
"""

# The collision layer (xbr extract --collision): the stage's collision
# glTF as a second instance. Its class nodes import as StaticBody3D with a
# concave shape plus a see-through debug mesh; C toggles the meshes (the
# shapes stay), and the "show_meshes" property does the same in the editor.
# E and "show_edges" toggle each class's crease lines (its <label>_edges
# child). F and "flatness" swap the class colours for the flatness bands
# (the <label>_flat child), O and "opaque" make the faces opaque; both
# start off. The instance's children are editable, so the Scene dock lists one
# node per class (col_<name>_0x<rrggbb> with [[extract.collision_class]])
# and its eye hides that class and its lines. The toggles fade a mesh out
# (GeometryInstance3D.transparency) and never set visible: hiding a class
# node would hide its lines too, and setting visible would undo the eye.
# A face faded to transparency 1.0 is still drawn and can still be picked
# in the editor, and the materials are shared, so a stage scene open twice
# shares O. The lines get a small shader that draws them a hair toward the
# camera (collision.EDGE_PULL): with O the faces write depth and the lines
# lying on them would flicker, and no_depth_test would show them through
# walls.
COLLISION_GD = f"""\
@tool
extends Node3D

@export var show_meshes := true:
	set(v):
		show_meshes = v
		_apply()

# The crease lines (collision.py's <class>{collision.EDGES} children). A class node
# hidden in the Scene dock hides its lines too, whatever this says.
@export var show_edges := true:
	set(v):
		show_edges = v
		_apply()

# The faces in flatness colours (the <class>{collision.FLAT} children: grey
# flat or vertical, yellow, orange, red the further off) instead of the
# class colours.
@export var flatness := false:
	set(v):
		flatness = v
		_apply()

# The faces fully opaque instead of see-through, still unlit.
@export var opaque := false:
	set(v):
		opaque = v
		_apply()


func _ready():
	_shade()
	_apply()


# collision.py bakes each face's shade into COLOR_0, but Godot 4.7.2's glTF
# import keeps the colours with vertex_color_use_as_albedo off, and the
# .import file's _subresources can only move a material out, not set its
# flags. So this sets it on the imported materials each time the scene
# loads, in the editor too (@tool). It is never saved: the glTF opened on
# its own, or a material extracted at reimport, shows flat colour.
func _shade():
	for mat in _materials():
		mat.vertex_color_use_as_albedo = true
	# The lines' own material, swapped in the same way: never saved.
	var sh = Shader.new()
	sh.code = EDGE_SHADER
	for e in _edges():
		var src = e.mesh.surface_get_material(0)
		if src is ShaderMaterial:
			continue
		var mat = ShaderMaterial.new()
		mat.shader = sh
		if src is BaseMaterial3D:
			mat.set_shader_parameter("colour", src.albedo_color)
		e.mesh.surface_set_material(0, mat)


# Unlit lines in one colour, each vertex pulled a fraction of its distance
# toward the camera, so they win the depth test against the faces they lie
# on and still lose it to anything nearer.
const EDGE_SHADER = '''shader_type spatial;
render_mode unshaded, cull_disabled;
uniform vec4 colour : source_color = vec4(1.0);

void vertex() {{
	vec4 v = MODELVIEW_MATRIX * vec4(VERTEX, 1.0);
	v.xyz *= 1.0 - {collision.EDGE_PULL};
	POSITION = PROJECTION_MATRIX * v;
}}

void fragment() {{
	ALBEDO = colour.rgb;
}}
'''


func _edges():
	return find_children("*{collision.EDGES}", "MeshInstance3D", true, false)


# The class meshes, not their lines or flatness copies.
func _faces():
	var out = []
	for m in find_children("*", "MeshInstance3D", true, false):
		var n = String(m.name)
		if m.mesh == null or n.ends_with("{collision.EDGES}") or n.ends_with("{collision.FLAT}"):
			continue
		out.append(m)
	return out


func _flats():
	return find_children("*{collision.FLAT}", "MeshInstance3D", true, false)


func _materials():
	var out = []
	for m in _faces() + _flats():
		for i in m.mesh.get_surface_count():
			var mat = m.mesh.surface_get_material(i)
			if mat is BaseMaterial3D:
				out.append(mat)
	return out


# Each toggle fades meshes (transparency) and never sets visible, so a
# class hidden in the Scene dock stays hidden and its children with it.
func _apply():
	if not is_inside_tree():
		return
	for m in _faces():
		m.transparency = 0.0 if show_meshes and not flatness else 1.0
	for m in _flats():
		m.transparency = 0.0 if show_meshes and flatness else 1.0
	for e in _edges():
		e.transparency = 0.0 if show_edges else 1.0
	for mat in _materials():
		if opaque:
			mat.transparency = BaseMaterial3D.TRANSPARENCY_DISABLED
		else:
			mat.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA


func _unhandled_input(event):
	if not (event is InputEventKey and event.pressed and not event.echo):
		return
	if event.keycode == KEY_C:
		show_meshes = not show_meshes
	elif event.keycode == KEY_E:
		show_edges = not show_edges
	elif event.keycode == KEY_F:
		flatness = not flatness
	elif event.keycode == KEY_O:
		opaque = not opaque
"""

SCENE_COLLISION = """\
[gd_scene load_steps=6 format=3]

[ext_resource type="Script" path="res://view.gd" id="1"]
[ext_resource type="PackedScene" path="res://stages/{name}/{name}.gltf" id="2"]
[ext_resource type="Script" path="res://stage.gd" id="3"]
[ext_resource type="PackedScene" path="res://stages/{name}/{name}_col.gltf" id="4"]
[ext_resource type="Script" path="res://collision.gd" id="5"]

[node name="{name}" type="Node3D"]
script = ExtResource("1")

[node name="stage" parent="." instance=ExtResource("2")]
script = ExtResource("3")

[node name="collision" parent="." instance=ExtResource("4")]
script = ExtResource("5")

[editable path="collision"]
"""

# The placements layer (xbr extract --placements): the stage's placements
# glTF as one more instance, plain nodes and meshes, no script.
PLACEMENTS_RESOURCE = (
    '[ext_resource type="PackedScene" path="res://stages/{name}/{name}_placements.gltf"'
    ' id="{id}"]\n'
)
PLACEMENTS_NODE = '\n[node name="placements" parent="." instance=ExtResource("{id}")]\n'


def scene(name, collision=False, placements=False):
    """A stage's .tscn: the stage, then its collision and placements
    layers when it has them."""
    text = (SCENE_COLLISION if collision else SCENE).format(name=name)
    if not placements:
        return text
    text, editable = (text.split("\n[editable ", 1) + [None])[:2]
    head, body = text.split("\n[node ", 1)
    # The next resource id and one more load step, whatever the template
    # above holds.
    n = 1 + max(int(i) for i in re.findall(r'^\[ext_resource [^\n]* id="(\d+)"\]$', head, re.M))
    head = re.sub(r"load_steps=(\d+)", lambda m: "load_steps=%d" % (int(m[1]) + 1), head, count=1)
    head += PLACEMENTS_RESOURCE.format(name=name, id=n)
    text = head + "\n[node " + body + PLACEMENTS_NODE.format(id=n)
    # Godot lists [editable] after every node.
    return text if editable is None else text + "\n[editable " + editable


GITIGNORE = "# Game data made by xbr extract: never commit it.\n*\n"


def write_project(out, title, stages, collision=(), placements=()):
    """project.godot, view.gd, stage.gd and one <stage>.tscn per stage;
    the glTF files are under stages/<name>/. A stage named in collision
    also instances its stages/<name>/<name>_col.gltf (and the project
    gets collision.gd); one named in placements its
    stages/<name>/<name>_placements.gltf."""
    os.makedirs(out, exist_ok=True)
    main = (stages[0] + ".tscn") if stages else ""
    files = {
        "project.godot": PROJECT.format(title=title.replace('"', "'"), main=main),
        "view.gd": VIEW_GD,
        "stage.gd": STAGE_GD,
        ".gitignore": GITIGNORE,
    }
    if collision:
        files["collision.gd"] = COLLISION_GD
    else:
        # A rerun without collision layers must not leave the script of an
        # earlier --collision run behind.
        stale = os.path.join(out, "collision.gd")
        if os.path.exists(stale):
            os.remove(stale)
    for name in stages:
        files[name + ".tscn"] = scene(name, name in collision, name in placements)
    for rel, text in files.items():
        with open(os.path.join(out, rel), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def import_project(binary, out, log_path):
    """Imports the project headless (no window, no sound); CliError when
    Godot fails or logs an error. Returns the log's lines."""
    cmd = [binary, "--headless", "--audio-driver", "Dummy", "--path", out, "--import"]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    log = r.stdout + r.stderr
    with open(log_path, "w", encoding="utf-8") as f:
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
