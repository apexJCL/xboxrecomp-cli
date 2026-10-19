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

# What the toolkit's enhancements layer defaults to (enhance.c, recomp_env.h).
WANT = {
    "render.scale": 1,
    "render.aniso": 0,
    "display.aspect": "4:3",
    "present.filter": "nearest",
    "present.fullscreen": False,
    "present.pacing": "sleep",
    "present.surface": "auto",
}


# Keys the private integration branch reads ahead of the public branch `new`
# pins (Hor+, display.overscan): the template gains them in the push round
# that publishes them.
AHEAD = {"display.movie", "display.overscan"}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_the_template_carries_the_toolkit_defaults():
    assert config_defaults.toolkit_defaults() == WANT
    text = config_defaults.template_text("Some Game")
    assert text.startswith("# Some Game options: every key at its default.")
    assert "@" not in text
    # No [textures] table: package --hud-pack refuses a file that has one.
    assert "textures" not in tomllib.loads(text)
    # Every value line has its comment at the same column, as the games'.
    for line in text.splitlines():
        if re.match(r"^[a-z_]+ = ", line):
            assert line.index("#") == 26, line
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
    with open(os.path.join(TOOLKIT, "src", "enhance", "enhance.c"), encoding="utf-8") as f:
        src = f.read()
    lists = {
        m.group(1): re.findall(r'"(\w+)"', m.group(2))
        for m in re.finditer(r"static const char \*const (s_\w+)\[\] = \{([^}]*)\}", src)
    }
    seen = {}
    for kind, key, arg in re.findall(
        r'enhance_cfg_(int|bool|choice|string)\("([a-z_]+\.[a-z_]+)",\s*([^)]*)\)', src
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
    # textures.* stay commented out in the template (--hud-pack adds them).
    keys = {k for k in seen if not k.startswith("textures.")}
    assert keys - AHEAD == set(WANT), sorted(keys ^ set(WANT))
    for key, want in WANT.items():
        assert key in seen, f"{key}: not read by this toolkit"
        assert seen[key] == want, key
