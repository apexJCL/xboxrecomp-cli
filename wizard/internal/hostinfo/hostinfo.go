// Package hostinfo is what the wizard knows about the machine it runs on:
// the OS in the CLI's words, the build folder (the root), the output
// folder, the settings file, free space and the Steam Deck.
package hostinfo

import (
	"encoding/json"
	"errors"
	"fmt"
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

// What an output folder may not overlap (OutClash).
const (
	ClashData = "data" // the game's data folder: saves and settings the wizard never touches
	ClashSrc  = "src"  // <build folder>/src: the clone, which a rebuild rewrites
	ClashDump = "dump" // the dump folder the build reads
)

// OutClash is which folder out is, holds or sits inside, or "" when none.
// data and dump may be "" (unknown yet); a dump that is a disc image file
// cannot clash, since bundles never go inside a file and a folder that
// merely holds the image is fine.
func OutClash(out, root, data, dump string) string {
	switch {
	case data != "" && Overlaps(out, data):
		return ClashData
	case Overlaps(out, filepath.Join(root, "src")):
		return ClashSrc
	case dump != "" && IsDir(dump) && Overlaps(out, dump):
		return ClashDump
	}
	return ""
}

// Settings is the wizard's own file: the chosen build folder (root, the
// key's name from before the output folder existed), the chosen output
// folder and the update-check switch, kept between runs. An empty Out is
// the default, worked out again on every run so a moved wizard is
// followed.
type Settings struct {
	Root          string `json:"root,omitempty"`
	Out           string `json:"out,omitempty"`
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

// existing is path or its nearest existing parent (a folder the build
// makes may not exist yet).
func existing(path string) (string, error) {
	p := path
	for {
		if _, err := os.Stat(p); err == nil {
			return p, nil
		} else if !errors.Is(err, fs.ErrNotExist) {
			return "", err
		}
		parent := filepath.Dir(p)
		if parent == p {
			return "", fs.ErrNotExist
		}
		p = parent
	}
}

// Free is the free space of the volume holding path, or of its nearest
// existing parent (the root may not exist yet).
func Free(path string) (uint64, error) {
	p, err := existing(path)
	if err != nil {
		return 0, err
	}
	return freeBytes(p)
}

// IsDir reports whether path is a folder that is there now. A saved
// output folder must be: it was when the player picked it, and a parent
// that exists says nothing, since /Volumes, /run/media/<user> and /mnt
// stay when the drive under them is unplugged.
func IsDir(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.IsDir()
}

// SameVolume reports whether a and b (or their nearest existing parents)
// are on one volume, so the build and the bundle draw on one free figure.
// An error on either side is "not known to be the same".
func SameVolume(a, b string) bool {
	pa, err := existing(a)
	if err != nil {
		return false
	}
	pb, err := existing(b)
	if err != nil {
		return false
	}
	return sameVolume(pa, pb)
}

// BundleNeed is the output folder's need for one bundle: the game files
// it carries and a GiB for the program, the launcher, symbols and slack.
func BundleNeed(dumpBytes uint64) uint64 { return dumpBytes + 1<<30 }

// TreeSize is the bytes of the files under dir (a game folder is a few
// hundred files).
func TreeSize(dir string) uint64 {
	var n uint64
	_ = filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err == nil && d.Type().IsRegular() {
			if fi, err := d.Info(); err == nil {
				n += uint64(fi.Size())
			}
		}
		return nil
	})
	return n
}

// ResolveOut is the output folder for a run: the saved one while it is
// there as a folder, else DefaultOut. why is for the log, "" when nothing was
// refused.
func ResolveOut(saved, root string) (dir, why string) {
	if saved != "" {
		if IsDir(saved) {
			return saved, ""
		}
		dir, why = DefaultOut(root)
		if why == "" {
			why = fmt.Sprintf("the saved output folder %s is not there (its drive may be unplugged)", saved)
		} else {
			why = fmt.Sprintf("the saved output folder %s is not there (its drive may be unplugged) and %s", saved, why)
		}
		return dir, why
	}
	return DefaultOut(root)
}

// DefaultOut is the output folder when the player chose none: dist beside
// the wizard (beside the .app on a Mac), else the build folder's dist when
// that place is temporary, a system folder or cannot be written. why says
// which, for the log; "" when the place beside the wizard is used.
func DefaultOut(root string) (dir, why string) {
	exe, err := os.Executable()
	if err == nil {
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		return filepath.Join(root, "dist"), fmt.Sprintf("cannot find the wizard's own folder: %v", err)
	}
	tmp := os.TempDir()
	if t, err := filepath.EvalSymlinks(tmp); err == nil {
		tmp = t
	}
	env := map[string]string{}
	for _, k := range []string{"ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemRoot"} {
		env[k] = os.Getenv(k)
	}
	return defaultOut(root, runtime.GOOS, exe, tmp, env)
}

// defaultOut is DefaultOut with the host's facts passed in; the write
// probe is real.
func defaultOut(root, goos, exe, tmp string, env map[string]string) (dir, why string) {
	cand, why := outCandidate(goos, exe, tmp, env)
	if why == "" {
		// Nothing lasting is made: the CLI creates dist itself.
		f, err := os.CreateTemp(cand, ".wizard-*")
		if err != nil {
			why = fmt.Sprintf("cannot write in %s", cand)
		} else {
			f.Close()
			os.Remove(f.Name())
		}
	}
	if why != "" {
		return filepath.Join(root, "dist"), why
	}
	return filepath.Join(cand, "dist"), ""
}

// outCandidate is the folder the wizard sits in (the one holding the .app
// on a Mac, not a folder inside it) and why it is refused as a place for
// the bundle: "" when it is not. exe is the resolved executable, tmp the
// resolved temporary folder, env the Windows folders' variables.
func outCandidate(goos, exe, tmp string, env map[string]string) (dir, why string) {
	dir = filepath.Dir(exe)
	if goos == "darwin" {
		for p := dir; ; p = filepath.Dir(p) {
			if strings.HasSuffix(strings.ToLower(p), ".app") {
				dir = filepath.Dir(p)
				break
			}
			if filepath.Dir(p) == p {
				break
			}
		}
	}
	fold := goos != "linux"
	// A zip's preview runs the exe from a temporary copy; macOS runs a
	// quarantined app from a randomised read-only path (translocation).
	if tmp != "" && within(dir, tmp, fold) ||
		goos == "darwin" && strings.Contains(exe, string(filepath.Separator)+"AppTranslocation"+string(filepath.Separator)) {
		return dir, fmt.Sprintf("%s is a temporary folder", dir)
	}
	var system []string
	switch goos {
	case "windows":
		for _, k := range []string{"ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "SystemRoot"} {
			if v := env[k]; v != "" {
				system = append(system, v)
			}
		}
	case "darwin":
		system = []string{"/Applications", "/System"}
	default:
		system = []string{"/usr", "/opt"}
	}
	for _, s := range system {
		if within(dir, s, fold) {
			return dir, fmt.Sprintf("%s is a system folder", dir)
		}
	}
	return dir, ""
}

// within reports whether p is dir or inside it, ignoring case when fold.
func within(p, dir string, fold bool) bool {
	p, dir = filepath.Clean(p), filepath.Clean(dir)
	if fold {
		p, dir = strings.ToLower(p), strings.ToLower(dir)
	}
	r, err := filepath.Rel(dir, p)
	return err == nil && (r == "." || filepath.IsLocal(r))
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
