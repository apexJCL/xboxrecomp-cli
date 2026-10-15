// Package fetch downloads by sha256 and unpacks: the player's tools (uv,
// MinGit, the Homebrew installer) from wizard/pins.json into
// <root>/tools/, and the game config's fetch entries into the clone.
// Nothing reaches its destination before its hash and size are checked.
package fetch

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"time"

	"wizard/internal/gamecfg"
)

type Asset struct {
	URL    string `json:"url"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size,omitempty"`
}

type Tool struct {
	Version string           `json:"version"`
	Assets  map[string]Asset `json:"assets"`
}

// Pins is the player's part of wizard/pins.json.
type Pins struct {
	UV              Tool  `json:"uv"`
	MinGit          Tool  `json:"mingit"`
	HomebrewInstall Asset `json:"homebrew_install"`
}

func LoadPins(b []byte) (*Pins, error) {
	var p Pins
	if err := json.Unmarshal(b, &p); err != nil {
		return nil, fmt.Errorf("pins.json: %w", err)
	}
	return &p, nil
}

// HostKey is the pins' asset key for this binary: GOOS-GOARCH.
func HostKey() string { return runtime.GOOS + "-" + runtime.GOARCH }

// Progress reports bytes done of total (total 0 when unknown).
type Progress func(done, total int64)

type hashWriter struct {
	w     io.Writer
	n     int64
	total int64
	p     Progress
	idle  *time.Timer
}

func (h *hashWriter) Write(b []byte) (int, error) {
	h.idle.Reset(IdleTimeout)
	n, err := h.w.Write(b)
	h.n += int64(n)
	if h.p != nil {
		h.p(h.n, h.total)
	}
	return n, err
}

// IdleTimeout is how long a download may go without a byte before it is
// dropped. A whole-request timeout would cut off a slow but live link on a
// large archive; a stalled one would otherwise hang the wizard for good.
var IdleTimeout = 60 * time.Second

var errStalled = errors.New("no data received for a while; check the connection and try again")

// Client is the HTTP client for downloads: bounded connect, TLS and header
// waits, with the body bounded by IdleTimeout instead.
func Client() *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DialContext = (&net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}).DialContext
	t.TLSHandshakeTimeout = 30 * time.Second
	t.ResponseHeaderTimeout = 60 * time.Second
	return &http.Client{Transport: t}
}

// Download fetches a.URL to dest: into a temp file beside it, checked for
// size and sha256, then renamed. On any failure nothing is left behind.
func Download(ctx context.Context, client *http.Client, a Asset, dest string, p Progress) (err error) {
	if a.SHA256 == "" {
		return fmt.Errorf("%s: no sha256 pinned", a.URL)
	}
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	idle := time.AfterFunc(IdleTimeout, func() { cancel(errStalled) })
	defer idle.Stop()
	defer func() {
		if err != nil && context.Cause(ctx) == errStalled {
			err = fmt.Errorf("%s: %w", a.URL, errStalled)
		}
	}()
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, a.URL, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s: HTTP %s", a.URL, resp.Status)
	}
	tmp, err := os.CreateTemp(filepath.Dir(dest), filepath.Base(dest)+".*.part")
	if err != nil {
		return err
	}
	defer func() {
		tmp.Close()
		if err != nil {
			os.Remove(tmp.Name())
		}
	}()
	sum := sha256.New()
	total := a.Size
	if total == 0 && resp.ContentLength > 0 {
		total = resp.ContentLength
	}
	hw := &hashWriter{w: io.MultiWriter(tmp, sum), total: total, p: p, idle: idle}
	if _, err = io.Copy(hw, resp.Body); err != nil {
		return fmt.Errorf("%s: %w", a.URL, err)
	}
	if a.Size != 0 && hw.n != a.Size {
		return fmt.Errorf("%s: got %d bytes, the pin says %d", a.URL, hw.n, a.Size)
	}
	if got := hex.EncodeToString(sum.Sum(nil)); got != strings.ToLower(a.SHA256) {
		return fmt.Errorf("%s: sha256 %s, the pin says %s", a.URL, got, a.SHA256)
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), dest)
}

// inside joins name under dir, refusing a path that would leave it. "."
// is dir itself (a tar's "./" entry); Entry refuses it as a dest first.
func inside(dir, name string) (string, error) {
	clean := filepath.Clean(filepath.FromSlash(strings.ReplaceAll(name, `\`, "/")))
	if !filepath.IsLocal(clean) {
		return "", fmt.Errorf("archive entry %q leaves the folder", name)
	}
	return filepath.Join(dir, clean), nil
}

// Unpack extracts a .zip, .tar.gz or .tar into dir (made aside, then moved in
// place). Links are refused: none of the pinned archives has one the
// wizard needs, and a link is how an archive writes outside its folder.
func Unpack(archive, dir string) error {
	part := dir + ".partial"
	os.RemoveAll(part)
	var err error
	switch {
	case strings.HasSuffix(archive, ".zip"):
		err = unzip(archive, part)
	case strings.HasSuffix(archive, ".tar.gz"), strings.HasSuffix(archive, ".tgz"):
		err = untar(archive, part, true)
	case strings.HasSuffix(archive, ".tar"):
		err = untar(archive, part, false)
	default:
		err = fmt.Errorf("%s: not a .zip, .tar.gz or .tar", filepath.Base(archive))
	}
	if err != nil {
		os.RemoveAll(part)
		return err
	}
	if err := os.RemoveAll(dir); err != nil {
		return err
	}
	return os.Rename(part, dir)
}

func writeFile(path string, r io.Reader, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, r); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func unzip(archive, dir string) error {
	z, err := zip.OpenReader(archive)
	if err != nil {
		return err
	}
	defer z.Close()
	for _, f := range z.File {
		out, err := inside(dir, f.Name)
		if err != nil {
			return err
		}
		mode := f.Mode()
		switch {
		case mode.IsDir():
			if err := os.MkdirAll(out, 0o755); err != nil {
				return err
			}
			continue
		case mode&os.ModeSymlink != 0:
			return fmt.Errorf("archive entry %q is a link", f.Name)
		}
		r, err := f.Open()
		if err != nil {
			return err
		}
		err = writeFile(out, r, mode.Perm()|0o600)
		r.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

func untar(archive, dir string, gzipped bool) error {
	f, err := os.Open(archive)
	if err != nil {
		return err
	}
	defer f.Close()
	var r io.Reader = f
	if gzipped {
		gz, err := gzip.NewReader(f)
		if err != nil {
			return err
		}
		r = gz
	}
	tr := tar.NewReader(r)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		out, err := inside(dir, h.Name)
		if err != nil {
			return err
		}
		switch h.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(out, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := writeFile(out, tr, os.FileMode(h.Mode).Perm()|0o600); err != nil {
				return err
			}
		case tar.TypeXGlobalHeader, tar.TypeXHeader:
		default:
			return fmt.Errorf("archive entry %q is not a file or folder", h.Name)
		}
	}
}

// Install downloads a and unpacks it into dir; a dir already holding a
// .sha256 marker with a's hash is kept as it is.
func Install(ctx context.Context, client *http.Client, a Asset, dir string, p Progress) error {
	mark := dir + ".sha256"
	if b, err := os.ReadFile(mark); err == nil && strings.TrimSpace(string(b)) == a.SHA256 {
		if _, err := os.Stat(dir); err == nil {
			return nil
		}
	}
	name := a.URL[strings.LastIndex(a.URL, "/")+1:]
	arc := filepath.Join(filepath.Dir(dir), "download-"+name)
	if err := Download(ctx, client, a, arc, p); err != nil {
		return err
	}
	defer os.Remove(arc)
	if err := Unpack(arc, dir); err != nil {
		return err
	}
	return os.WriteFile(mark, []byte(a.SHA256+"\n"), 0o644)
}

// UVMin is the oldest uv the CLI runs with (env.UV_MIN).
var UVMin = []int{0, 5, 31}

var uvVersionRE = regexp.MustCompile(`uv (\d+)\.(\d+)\.(\d+)`)

// ParseUVVersion reads `uv --version` output.
func ParseUVVersion(s string) ([]int, bool) {
	m := uvVersionRE.FindStringSubmatch(s)
	if m == nil {
		return nil, false
	}
	v := make([]int, 3)
	for i := range v {
		v[i], _ = strconv.Atoi(m[i+1])
	}
	return v, true
}

// UVOnPath is the uv on PATH when it is new enough, else "".
func UVOnPath() string {
	uv, err := exec.LookPath("uv")
	if err != nil {
		return ""
	}
	out, err := exec.Command(uv, "--version").Output()
	if err != nil {
		return ""
	}
	v, ok := ParseUVVersion(string(out))
	if !ok || slices.Compare(v, UVMin) < 0 {
		return ""
	}
	return uv
}

// Find is the first file named name under dir (the archives keep uv in a
// folder named after the platform).
func Find(dir, name string) string {
	var found string
	filepath.WalkDir(dir, func(p string, d os.DirEntry, err error) error {
		if err == nil && !d.IsDir() && d.Name() == name && found == "" {
			found = p
		}
		return nil
	})
	return found
}

// ForHost is the fetch entries for a host (an entry with no hosts is for
// every host).
func ForHost(entries []gamecfg.Fetch, host string) []gamecfg.Fetch {
	var out []gamecfg.Fetch
	for _, e := range entries {
		if len(e.Hosts) == 0 || slices.Contains(e.Hosts, host) {
			out = append(out, e)
		}
	}
	return out
}

// Entry fetches one config fetch entry into the clone at its dest: unpacked
// there, or with unpack = false saved as that file.
func Entry(ctx context.Context, client *http.Client, e gamecfg.Fetch, clone string, p Progress) error {
	if !gamecfg.LocalDest(e.Dest) {
		return fmt.Errorf("fetch %s: dest %q is not a path inside the clone", e.Name, e.Dest)
	}
	dest, err := inside(clone, e.Dest)
	if err != nil {
		return err
	}
	a := Asset{URL: e.URL, SHA256: e.SHA256, Size: e.Size}
	if e.Unpacks() {
		return Install(ctx, client, a, dest, p)
	}
	if sum, err := fileSHA256(dest); err == nil && sum == strings.ToLower(a.SHA256) {
		return nil
	}
	return Download(ctx, client, a, dest, p)
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
