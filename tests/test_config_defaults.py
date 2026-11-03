"""The toolkit's option defaults the CLI holds (config_defaults): the
config.default.toml `new` writes, and doctor's line on where a game's own
file differs. The real toolkit is read when a checkout is beside this one,
so a default that moves there shows up here.

  uv run pytest tests/test_config_defaults.py
"""

import os
import re
import tomllib

import pytest

from xboxrecomp_cli import config_defaults

HERE = os.path.dirname(os.path.abspath(__file__))
# As test_scaffold finds it: $XBOXRECOMP_DIR, else a checkout beside this one.
TOOLKIT = next(
    (
        p
        for p in (
            os.environ.get("XBOXRECOMP_DIR", ""),
            os.path.join(HERE, "..", "..", "xboxrecomp"),
        )
        if p and os.path.isfile(os.path.join(p, "src", "enhance", "enhance.c"))
    ),
    "",
)

# What the toolkit's enhancements layer defaults to (enhance.c, mod.c for
# menu.enabled, recomp_env.h).
WANT = {
    "render.scale": 1,
    "render.aniso": 0,
    "render.snap_2d": False,
    "display.aspect": "4:3",
    "display.movie": "black",
    "display.overscan": 0,
    "display.hud_aspect": "display",
    "display.hud_margin": 0,
    "present.filter": "nearest",
    "present.fullscreen": False,
    "present.pacing": "sleep",
    "present.surface": "auto",
    "present.shader_lines": "auto",
    "storage.ram_cache": True,
    "system.language": "en",
    "menu.enabled": False,
}

# Keys the template names only in a comment, as the [textures] table: their
# stock is unset or "none" (the build's own librashader; no shader, no
# profile), so the line shows the key without setting it. present.shader and
# present.profile are read outside enhance.c (shader_profile_req.c),
# present.shader_lib inside it.
COMMENTED = {"present.shader", "present.profile", "present.shader_lib"}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_the_template_carries_the_toolkit_defaults():
    assert config_defaults.toolkit_defaults() == WANT
    # The ports' own: the template ships the menu on, as every game does.
    assert config_defaults.template_defaults() == {**WANT, "menu.enabled": True}
    text = config_defaults.template_text("Some Game")
    assert text.startswith("# Some Game options: every key at its default.")
    assert "@" not in text
    # No [textures] table: package --hud-pack refuses a file that has one.
    assert "textures" not in tomllib.loads(text)
    for key in COMMENTED:
        table, name = key.split(".")
        section = text.split("[%s]" % table, 1)[1].split("\n[", 1)[0]
        assert re.search(r"^# %s = " % name, section, re.M), key
    # Every value line has its comment at the same column, as the games'.
    for line in text.splitlines():
        if re.match(r"^[a-z_0-9]+ = ", line):
            assert line.index("#") == 26, line
        elif re.match(r"^# [a-z_0-9]+ = ", line):
            assert line.index("#", 1) == 26, line
        elif line.startswith(" ") and line.strip():
            assert line.index("#") == 26, line


def test_drift_names_only_keys_set_to_another_value(tmp_path):
    p = str(tmp_path / "config.default.toml")
    write(p, config_defaults.template_text("G"))
    assert config_defaults.drift(p) == []
    assert config_defaults.doctor_line(p) == "config.default.toml: the toolkit's defaults"
    # Another value, a key left out, a game's own key, an [input] key.
    write(
        p,
        '[render]\nscale = 1\n[present]\npacing = "spin"\nfullscreen = 0\n'
        '[fps]\nmode = "lock30"\n[input]\nkeyboard = true\n',
    )
    assert config_defaults.drift(p) == [
        ("present.fullscreen", 0, False),
        ("present.pacing", "spin", "sleep"),
    ]
    assert config_defaults.doctor_line(p) == (
        "config.default.toml: differs from the toolkit's defaults: "
        'present.fullscreen = 0 (toolkit false), present.pacing = "spin" (toolkit "sleep")'
    )
    # The menu on, as every game ships it, is no drift; off is, against the
    # ports' value rather than the toolkit's.
    write(p, "[menu]\nenabled = true\n")
    assert config_defaults.drift(p) == []
    write(p, "[menu]\nenabled = false\n")
    assert config_defaults.doctor_line(p) == (
        "config.default.toml: differs from the toolkit's defaults: "
        "menu.enabled = false (new games true)"
    )


def test_doctor_line_without_a_file_or_with_a_bad_one(tmp_path):
    assert config_defaults.doctor_line(str(tmp_path / "none.toml")) == ""
    p = str(tmp_path / "config.default.toml")
    write(p, "[present\n")
    assert config_defaults.doctor_line(p).startswith("config.default.toml: unreadable (")


@pytest.mark.skipif(
    not TOOLKIT,
    reason="no toolkit checkout beside the CLI",
)
def test_the_defaults_match_the_toolkit_beside():
    """Each key's default in the toolkit's enhance.c, as the code passes it."""
    enhance = os.path.join(TOOLKIT, "src", "enhance")
    with open(os.path.join(enhance, "enhance.c"), encoding="utf-8") as f:
        src = f.read()
    lists = {
        m.group(1): re.findall(r'"(\w+)"', m.group(2))
        for m in re.finditer(r"static const char \*const (s_\w+)\[\] = \{([^}]*)\}", src)
    }
    seen = {}
    for kind, key, arg in re.findall(
        r'enhance_cfg_(int|bool|choice|string)\("([a-z_]+\.[a-z_0-9]+)",\s*([^)]*)\)', src
    ):
        if kind == "choice":
            table, dflt = [x.strip() for x in arg.split(",")]
            words = lists[table]
            if dflt.isdigit():
                seen[key] = words[int(dflt)]
            else:
                # An enum default (XBOX_PACING_SLEEP) names its word last, as
                # the lists' "Indexes match ..." comments pair them.
                word = dflt.rsplit("_", 1)[-1].lower()
                if word not in words:
                    pytest.fail("%s: enum %s has no word in %s %s" % (key, dflt, table, words))
                seen[key] = word
        elif kind == "bool":
            seen[key] = arg.strip() != "0"
        elif kind == "int":
            seen[key] = int(arg)
        else:
            seen[key] = arg.strip().strip('"')
    # menu.enabled is read by the mods layer (mod.c), not enhance.c.
    with open(os.path.join(enhance, "mod.c"), encoding="utf-8") as f:
        menu = re.findall(r'enhance_cfg_bool\("menu\.enabled",\s*([^)]*)\)', f.read())
    assert menu, "menu.enabled: not read by this toolkit's mod.c"
    seen["menu.enabled"] = menu[0].strip() != "0"
    # Every key any enhance source names, for the commented lines' keys.
    named = set()
    for name in os.listdir(enhance):
        if name.endswith(".c"):
            with open(os.path.join(enhance, name), encoding="utf-8") as f:
                named.update(re.findall(r'enhance_cfg_\w+\("([a-z_]+\.[a-z_0-9]+)"', f.read()))
    assert COMMENTED <= named, sorted(COMMENTED - named)
    # textures.* stay commented out in the template (--hud-pack adds them).
    keys = {k for k in seen if not k.startswith("textures.")}
    assert keys - COMMENTED == set(WANT), sorted((keys - COMMENTED) ^ set(WANT))
    for key, want in WANT.items():
        assert key in seen, f"{key}: not read by this toolkit"
        assert seen[key] == want, key
