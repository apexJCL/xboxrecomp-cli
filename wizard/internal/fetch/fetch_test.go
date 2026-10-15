package fetch

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"wizard"
	"wizard/internal/gamecfg"
)

func sum(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func serve(t *testing.T, files map[string][]byte) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, ok := files[r.URL.Path]
		if !ok {
			http.NotFound(w, r)
			return
		}
		w.Write(b)
	}))
	t.Cleanup(s.Close)
	return s
}

func leftovers(t *testing.T, dir string) []string {
	t.Helper()
	ents, _ := os.ReadDir(dir)
	var out []string
	for _, e := range ents {
		out = append(out, e.Name())
	}
	return out
}

func TestDownload(t *testing.T) {
	body := []byte("the uv archive")
	s := serve(t, map[string][]byte{"/a": body})
	ctx := context.Background()
	cases := []struct {
		name, want string
		a          Asset
	}{
		{"right hash", "", Asset{URL: s.URL + "/a", SHA256: sum(body), Size: int64(len(body))}},
		{"wrong hash", "sha256", Asset{URL: s.URL + "/a", SHA256: sum([]byte("x"))}},
		{"truncated", "bytes", Asset{URL: s.URL + "/a", SHA256: sum(body), Size: int64(len(body)) + 10}},
		{"missing", "404", Asset{URL: s.URL + "/b", SHA256: sum(body)}},
		{"no pin", "no sha256", Asset{URL: s.URL + "/a"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			dest := filepath.Join(dir, "out")
			err := Download(ctx, s.Client(), tc.a, dest, nil)
			if tc.want == "" {
				if err != nil {
					t.Fatal(err)
				}
				got, _ := os.ReadFile(dest)
				if !bytes.Equal(got, body) {
					t.Fatal("content")
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("got %v, want %q", err, tc.want)
			}
			if l := leftovers(t, dir); len(l) != 0 {
				t.Fatalf("left behind: %v", l)
			}
		})
	}
}

func tgz(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var b bytes.Buffer
	gz := gzip.NewWriter(&b)
	tw := tar.NewWriter(gz)
	for name, body := range files {
		tw.WriteHeader(&tar.Header{Name: name, Mode: 0o755, Size: int64(len(body)), Typeflag: tar.TypeReg})
		tw.Write([]byte(body))
	}
	tw.Close()
	gz.Close()
	return b.Bytes()
}

func zipped(t *testing.T, files map[string]string) []byte {
	t.Helper()
	var b bytes.Buffer
	zw := zip.NewWriter(&b)
	for name, body := range files {
		w, _ := zw.Create(name)
		w.Write([]byte(body))
	}
	zw.Close()
	return b.Bytes()
}

func TestInstallAndUnpack(t *testing.T) {
	tg := tgz(t, map[string]string{"uv-aarch64-apple-darwin/uv": "bin"})
	zp := zipped(t, map[string]string{"cmd/git.exe": "exe"})
	s := serve(t, map[string][]byte{"/uv.tar.gz": tg, "/git.zip": zp})
	root := t.TempDir()
	ctx := context.Background()
	if err := Install(ctx, s.Client(), Asset{URL: s.URL + "/uv.tar.gz", SHA256: sum(tg)}, filepath.Join(root, "uv"), nil); err != nil {
		t.Fatal(err)
	}
	uv := Find(filepath.Join(root, "uv"), "uv")
	if st, err := os.Stat(uv); err != nil || st.Mode().Perm()&0o100 == 0 {
		t.Fatalf("uv %q %v", uv, err)
	}
	if err := Install(ctx, s.Client(), Asset{URL: s.URL + "/git.zip", SHA256: sum(zp)}, filepath.Join(root, "git"), nil); err != nil {
		t.Fatal(err)
	}
	if Find(filepath.Join(root, "git"), "git.exe") == "" {
		t.Fatal("no git.exe")
	}
	// A second install with the same pin is a no-op (the server is gone).
	s.Close()
	if err := Install(ctx, http.DefaultClient, Asset{URL: s.URL + "/uv.tar.gz", SHA256: sum(tg)}, filepath.Join(root, "uv"), nil); err != nil {
		t.Fatal(err)
	}
}

func TestUnpackRefusesEscape(t *testing.T) {
	dir := t.TempDir()
	arc := filepath.Join(dir, "bad.tar.gz")
	os.WriteFile(arc, tgz(t, map[string]string{"../evil": "x"}), 0o644)
	if err := Unpack(arc, filepath.Join(dir, "out")); err == nil || !strings.Contains(err.Error(), "leaves") {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "evil")); err == nil {
		t.Fatal("wrote outside")
	}
	if l := leftovers(t, dir); len(l) != 1 {
		t.Fatalf("left behind: %v", l)
	}
}

func TestForHost(t *testing.T) {
	es := []gamecfg.Fetch{
		{Name: "all"},
		{Name: "win", Hosts: []string{"windows"}},
		{Name: "unix", Hosts: []string{"macos", "linux"}},
	}
	var names []string
	for _, e := range ForHost(es, "linux") {
		names = append(names, e.Name)
	}
	if strings.Join(names, ",") != "all,unix" {
		t.Fatal(names)
	}
}

func TestParseUVVersion(t *testing.T) {
	v, ok := ParseUVVersion("uv 0.13.1 (Homebrew 2026-10-01)\n")
	if !ok || v[1] != 13 {
		t.Fatal(v)
	}
	if _, ok := ParseUVVersion("nope"); ok {
		t.Fatal("parsed nothing")
	}
}

func TestEmbeddedPins(t *testing.T) {
	p, err := LoadPins(wizard.PinsJSON)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"darwin-arm64", "darwin-amd64", "linux-amd64", "windows-amd64"} {
		if a := p.UV.Assets[k]; len(a.SHA256) != 64 || a.Size == 0 {
			t.Errorf("uv %s: %+v", k, a)
		}
	}
	if len(p.MinGit.Assets["windows-amd64"].SHA256) != 64 || len(p.HomebrewInstall.SHA256) != 64 {
		t.Error("mingit or homebrew_install unpinned")
	}
}

func TestDownloadStalls(t *testing.T) {
	release := make(chan struct{})
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("part"))
		w.(http.Flusher).Flush()
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(func() { close(release); s.Close() })
	old := IdleTimeout
	IdleTimeout = 200 * time.Millisecond
	t.Cleanup(func() { IdleTimeout = old })
	dir := t.TempDir()
	err := Download(context.Background(), Client(), Asset{URL: s.URL + "/a", SHA256: sum([]byte("x"))}, filepath.Join(dir, "out"), nil)
	if err == nil || !strings.Contains(err.Error(), "no data received") {
		t.Fatalf("got %v, want a stall error", err)
	}
	if l := leftovers(t, dir); len(l) != 0 {
		t.Fatalf("left behind: %v", l)
	}
}

func TestEntry(t *testing.T) {
	zp := zipped(t, map[string]string{"pack/a.txt": "a"})
	raw := []byte("one file")
	s := serve(t, map[string][]byte{"/p.zip": zp, "/f.bin": raw})
	clone := t.TempDir()
	ctx := context.Background()
	no := false
	if err := Entry(ctx, s.Client(), gamecfg.Fetch{Name: "p", URL: s.URL + "/p.zip", SHA256: sum(zp), Dest: "third_party/p"}, clone, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(clone, "third_party", "p", "pack", "a.txt")); err != nil {
		t.Fatal(err)
	}
	f := gamecfg.Fetch{Name: "f", URL: s.URL + "/f.bin", SHA256: sum(raw), Dest: "data/f.bin", Unpack: &no}
	if err := Entry(ctx, s.Client(), f, clone, nil); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(filepath.Join(clone, "data", "f.bin")); !bytes.Equal(got, raw) {
		t.Fatalf("file %q", got)
	}
	// Already there with the pinned hash: kept without a download.
	s.Close()
	if err := Entry(ctx, http.DefaultClient, f, clone, nil); err != nil {
		t.Fatal(err)
	}
	for _, dest := range []string{"", ".", "..", "../x", "a/../../x", "/abs"} {
		e := gamecfg.Fetch{Name: "bad", URL: s.URL + "/p.zip", SHA256: sum(zp), Dest: dest}
		if err := Entry(ctx, http.DefaultClient, e, clone, nil); err == nil || !strings.Contains(err.Error(), "inside the clone") {
			t.Fatalf("dest %q: got %v", dest, err)
		}
	}
}
