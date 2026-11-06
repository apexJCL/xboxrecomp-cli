"""The game README skeleton (templates/readme/, docs/readme.md): the issue
form and its config are YAML a GitHub form takes, the skeleton uses the
tokens the standard lists and no others, and no player-facing text names a
config.toml key or an environment variable.

  uv run pytest tests/test_readme_template.py
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "src", "xboxrecomp_cli", "templates", "readme")
FORMS = os.path.join(TPL, ".github", "ISSUE_TEMPLATE")
TOKEN = re.compile(r"@[A-Z][A-Z0-9_]*@")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def template_files():
    for dirpath, _, names in os.walk(TPL):
        for n in sorted(names):
            yield os.path.join(dirpath, n)


def load_yaml(text):
    """PyYAML when the environment has it; the dev group keeps to the stdlib,
    so otherwise the checks below fall back to the form's line structure."""
    try:
        import yaml
    except ImportError:
        return None
    return yaml.safe_load(text)


@pytest.mark.parametrize("name", ["bug_report.yml", "config.yml"])
def test_form_is_yaml(name):
    text = read(FORMS, name)
    assert "\t" not in text, "YAML takes no tabs"
    data = load_yaml(text)
    if data is not None:
        assert isinstance(data, dict)
        if name == "bug_report.yml":
            assert {"name", "description", "body"} <= set(data)
            ids = [b["id"] for b in data["body"] if "id" in b]
            assert len(ids) == len(set(ids))
        else:
            assert "blank_issues_enabled" in data
        return
    # Stdlib fallback: even indentation, and every body item a known type.
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        assert indent % 2 == 0, line
    if name == "bug_report.yml":
        top = {m.group(1) for m in re.finditer(r"^([a-z_]+):", text, re.M)}
        assert {"name", "description", "body"} <= top
        types = re.findall(r"^  - type: (\S+)$", text, re.M)
        assert types and set(types) <= {"markdown", "textarea", "input", "dropdown", "checkboxes"}
        ids = re.findall(r"^    id: (\S+)$", text, re.M)
        assert len(ids) == len(set(ids))
    else:
        assert re.search(r"^blank_issues_enabled: (true|false)$", text, re.M)


def test_tokens_match_the_standard():
    doc = read(ROOT, "docs", "readme.md")
    start = doc.index("The tokens are")
    listed = set(TOKEN.findall(doc[start : doc.index("\n\n", start)]))
    used = set()
    for path in template_files():
        used |= set(TOKEN.findall(read(path)))
    assert used == listed


def test_no_config_keys_or_env_vars():
    key = re.compile(r"\b(render|display|present|input|system|mods|pack|audio|kernel)\.[a-z_]+\b")
    for path in template_files():
        text = re.sub(r"<!--.*?-->", "", read(path), flags=re.S)
        assert "RECOMP_" not in text, path
        assert "config.toml" not in text, path
        assert not key.search(text), (path, key.search(text))
