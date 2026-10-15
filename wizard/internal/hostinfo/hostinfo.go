// Package hostinfo is what the wizard knows about the machine it runs on:
// the OS in the CLI's words, the install root, the settings file, free
// space and the Steam Deck.
package hostinfo

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// OS is the host in the wizard's words: windows, macos or linux.
func OS() string {
	switch runtime.GOOS {
	case "darwin":
		return "macos"
	case "windows":
		return "windows"
	}
	return "linux"
}

// NativeTarget is the package target of this host (the CLI's
// main.native_target rule: steamos for any Linux).
func NativeTarget() string {
	if o := OS(); o != "linux" {
		return o
	}
	return "steamos"
}

// DefaultRoot is where the clone and the tools go unless the player picks
// another folder: <slug>-recomp with the user's application data. The
// clone inside it is a short "src" (Windows' 260-character paths). The
// name is never the bare slug, which a game's own data folder may use
// (Windows folder names ignore case).
func DefaultRoot(slug string) string {
	home, _ := os.UserHomeDir()
	switch OS() {
	case "windows":
		base := os.Getenv("LOCALAPPDATA")
		if base == "" {
			base = filepath.Join(home, "AppData", "Local")
		}
		return filepath.Join(base, slug+"-recomp")
	case "macos":
		return filepath.Join(home, "Library", "Application Support", slug+"-recomp")
	}
	if d := os.Getenv("XDG_DATA_HOME"); d != "" {
		return filepath.Join(d, slug+"-recomp")
	}
	return filepath.Join(home, ".local", "share", slug+"-recomp")
}

// ExpandData turns a game.toml data folder (~/..., %VAR%\...) into a path
// on this host; "" when a variable it names is unset.
func ExpandData(p string) string {
	if rest, ok := strings.CutPrefix(p, "~/"); ok {
		home, err := os.UserHomeDir()
		if err != nil {
			return ""
		}
		return filepath.Join(home, filepath.FromSlash(rest))
	}
	if strings.HasPrefix(p, "%") {
		name, rest, ok := strings.Cut(p[1:], "%")
		v := os.Getenv(name)
		if !ok || v == "" {
			return ""
		}
		return filepath.Join(v, filepath.FromSlash(strings.ReplaceAll(rest, `\`, "/")))
	}
	return filepath.Clean(p)
}

// Overlaps reports whether a and b are the same folder or one holds the
// other, ignoring case where the host's file names do (Windows, macOS).
func Overlaps(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	if OS() != "linux" {
		a, b = strings.ToLower(a), strings.ToLower(b)
	}
	inside := func(x, y string) bool {
		r, err := filepath.Rel(y, x)
		return err == nil && filepath.IsLocal(r)
	}
	return inside(a, b) || inside(b, a)
}

// Settings is the wizard's own file: the chosen root and the update-check
// switch, kept between runs.
type Settings struct {
	Root          string `json:"root,omitempty"`
	NoUpdateCheck bool   `json:"no_update_check,omitempty"`
}

// SettingsDir is <user config dir>/<slug>-wizard: %APPDATA% on Windows,
// ~/Library/Application Support on a Mac, ~/.config on Linux.
func SettingsDir(slug string) string {
	d, err := os.UserConfigDir()
	if err != nil {
		d = os.TempDir()
	}
	return filepath.Join(d, slug+"-wizard")
}

// LoadSettings reads the file; a missing or unreadable one is the
// defaults, never an error a player sees.
func LoadSettings(dir string) Settings {
	var s Settings
	b, err := os.ReadFile(filepath.Join(dir, "settings.json"))
	if err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func SaveSettings(dir string, s Settings) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	tmp := filepath.Join(dir, "settings.json.tmp")
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(dir, "settings.json"))
}

// NeedBytes is the free space the build wants before the clone (doctor's
// figure).
const NeedBytes = 15 << 30

// Free is the free space of the volume holding path, or of its nearest
// existing parent (the root may not exist yet).
func Free(path string) (uint64, error) {
	p := path
	for {
		if _, err := os.Stat(p); err == nil {
			return freeBytes(p)
		} else if !errors.Is(err, fs.ErrNotExist) {
			return 0, err
		}
		parent := filepath.Dir(p)
		if parent == p {
			return 0, fs.ErrNotExist
		}
		p = parent
	}
}

// OSRelease is /etc/os-release's KEY=value lines.
func OSRelease(text string) map[string]string {
	out := map[string]string{}
	for _, line := range strings.Split(text, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		if ok {
			out[k] = strings.Trim(v, `"'`)
		}
	}
	return out
}

func readOSRelease() map[string]string {
	b, err := os.ReadFile("/etc/os-release")
	if err != nil {
		return map[string]string{}
	}
	return OSRelease(string(b))
}

// Flavour is doctor.linux_flavour's mapping: immutable (SteamOS, an
// ostree host), fedora, arch or debian.
func Flavour(rel map[string]string, ostree bool) string {
	ids := strings.ToLower(rel["ID"] + " " + rel["ID_LIKE"])
	if ostree || strings.Contains(ids, "steamos") {
		return "immutable"
	}
	for _, k := range []string{"fedora", "arch", "debian", "ubuntu"} {
		if strings.Contains(ids, k) {
			if k == "ubuntu" {
				return "debian"
			}
			return k
		}
	}
	return "debian"
}

// LinuxFlavour is this host's Flavour.
func LinuxFlavour() string {
	_, err := os.Stat("/run/ostree-booted")
	return Flavour(readOSRelease(), err == nil)
}

// IsDeck reports a SteamOS host: the build is long and memory-heavy there.
func IsDeck() bool {
	return OS() == "linux" && strings.Contains(strings.ToLower(readOSRelease()["ID"]), "steamos")
}

// GitHint is the command that installs git on this Linux flavour.
func GitHint(flavour string) string {
	switch flavour {
	case "fedora":
		return "sudo dnf install git"
	case "arch":
		return "sudo pacman -S git"
	case "immutable":
		return "git comes with this system; if it is missing, install it in a distrobox"
	}
	return "sudo apt install git"
}
