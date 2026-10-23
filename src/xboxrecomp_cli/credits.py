"""The open-source credits the build wizard shows (docs/wizard.md, Credits).

The CLI owns the list and its order, so every game's wizard gets it
without configuring it: our repos first, then the third-party projects.
Ours are the game's own repo (its [[wizard.credit]] entries marked own),
then this CLI, the toolkit and the toolkit's upstream. The third-party
projects are the game's other entries (a font only it uses), then the
ones the wizard and the build use directly. A game that lists nothing
gets the generic order, this CLI first. Transitive dependencies are not
listed: their licences are in the NOTICES.txt each wizard archive
carries.
"""


def _c(name, role, license, url):
    return {"name": name, "role": role, "license": license, "url": url}


OURS = (
    _c(
        "xboxrecomp-cli",
        "this build wizard and the CLI that runs the build",
        "MIT",
        "https://github.com/apexJCL/xboxrecomp-cli",
    ),
    _c(
        "xboxrecomp",
        "the recompilation toolkit (this fork)",
        "MIT",
        "https://github.com/apexJCL/xboxrecomp",
    ),
    _c(
        "xboxrecomp (upstream)",
        "the original toolkit, by sp00nz",
        "MIT",
        "https://github.com/sp00nznet/xboxrecomp",
    ),
)

# In three groups: what the wizard is made with, what goes into the game
# the build makes, then the build's tools. Each is used directly (the
# wizard-credits design, D6, says where).
THIRD_PARTY = (
    _c("Go", "the wizard's language and runtime", "BSD-3-Clause", "https://github.com/golang/go"),
    _c("Fyne", "the wizard's window toolkit", "BSD-3-Clause", "https://github.com/fyne-io/fyne"),
    _c("zenity", "the wizard's file dialogs", "MIT", "https://github.com/ncruces/zenity"),
    _c("Tabler Icons", "the wizard's icons", "MIT", "https://github.com/tabler/tabler-icons"),
    _c(
        "xemu",
        "the toolkit's audio (MCPX APU) and NV2A register code",
        "LGPL-2.1-or-later",
        "https://github.com/xemu-project/xemu",
    ),
    _c(
        "dsp56300",
        "the APU's DSP, in games built with XBOXRECOMP_APU_DSP",
        "MIT",
        "https://github.com/xemu-project/dsp56300",
    ),
    _c("SDL", "window, input and sound on macOS", "Zlib", "https://github.com/libsdl-org/SDL"),
    _c(
        "OpenSSL",
        "the Xbox kernel's crypto on macOS",
        "Apache-2.0",
        "https://github.com/openssl/openssl",
    ),
    _c("libepoxy", "OpenGL loading on macOS", "MIT", "https://github.com/anholt/libepoxy"),
    _c(
        "Python",
        "runs the CLI and the toolkit's tools",
        "PSF-2.0",
        "https://github.com/python/cpython",
    ),
    _c(
        "uv",
        "installs Python and the build's tools",
        "MIT OR Apache-2.0",
        "https://github.com/astral-sh/uv",
    ),
    _c(
        "Git",
        "fetches the game, the toolkit and the CLI (MinGit on Windows)",
        "GPL-2.0-only",
        "https://github.com/git/git",
    ),
    _c(
        "Homebrew",
        "installs the macOS libraries",
        "BSD-2-Clause",
        "https://github.com/Homebrew/brew",
    ),
    _c("CMake", "configures the game's build", "BSD-3-Clause", "https://github.com/Kitware/CMake"),
    _c("Ninja", "runs the game's build", "Apache-2.0", "https://github.com/ninja-build/ninja"),
    _c(
        "llvm-mingw",
        "the Windows compiler",
        "ISC AND Apache-2.0 WITH LLVM-exception",
        "https://github.com/mstorsjo/llvm-mingw",
    ),
    # Not on GitHub: its official site.
    _c("NSIS", "makes the Windows installer", "Zlib", "https://nsis.sourceforge.io/"),
    _c(
        "Capstone",
        "decodes the game's code for the lifter",
        "BSD-3-Clause",
        "https://github.com/capstone-engine/capstone",
    ),
    _c("pefile", "reads the game's executable", "MIT", "https://github.com/erocarrera/pefile"),
    _c(
        "umu-launcher",
        "runs the Windows build on SteamOS",
        "GPL-3.0-only",
        "https://github.com/Open-Wine-Components/umu-launcher",
    ),
)


def _key(url):
    return url.rstrip("/").lower()


def merged(game=()):
    """The wizard's list: the game's own entries, OURS, the game's other
    entries, then THIRD_PARTY, each in its given order. The game's other
    entries lead the third-party projects rather than being sorted into
    them: THIRD_PARTY is grouped by what uses it (the wizard, the game,
    the build's tools), and a game's extra belongs to none of those
    groups. A url is listed once, where it first comes, so a game that
    also credits, say, SDL lists it once, as the game wrote it."""
    own = [c for c in game if c.get("own")]
    extra = [c for c in game if not c.get("own")]
    seen, out = set(), []
    for c in [*own, *OURS, *extra, *THIRD_PARTY]:
        if _key(c["url"]) not in seen:
            seen.add(_key(c["url"]))
            # own only picks the place; the config's entries are the four
            # fields the wizard shows.
            out.append({k: c[k] for k in ("name", "role", "license", "url")})
    return out
