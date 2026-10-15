package main

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"testing"
)

// The committed SVG renders as a tile with transparent corners, the .ico
// holds its five PNG entries, and the 256 render is the committed embed.
func TestIcongen(t *testing.T) {
	out := t.TempDir()
	if err := run([]string{"-in", "../../icon/wizard-icon.svg", "-out", out}, nil); err != nil {
		t.Fatal(err)
	}
	tile := color.NRGBA{R: 0x1e, G: 0x4d, B: 0x6b, A: 0xff}
	for _, n := range []int{32, 256} {
		b, err := os.ReadFile(filepath.Join(out, fmt.Sprintf("icon-%d.png", n)))
		if err != nil {
			t.Fatal(err)
		}
		img, err := png.Decode(bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		if s := img.Bounds().Dx(); s != n {
			t.Fatalf("icon-%d is %d wide", n, s)
		}
		// Left of the centre, under the gamepad's body: the tile, not
		// the glyph; the corner is outside the rounded tile.
		if c := color.NRGBAModel.Convert(img.At(n/10, n/2)).(color.NRGBA); c != tile {
			t.Errorf("icon-%d: the tile is %v, want %v", n, c, tile)
		}
		if _, _, _, a := img.At(0, 0).RGBA(); a != 0 {
			t.Errorf("icon-%d: the corner's alpha is %d", n, a)
		}
	}
	ico, err := os.ReadFile(filepath.Join(out, "wizard.ico"))
	if err != nil {
		t.Fatal(err)
	}
	var head struct{ Reserved, Type, Count uint16 }
	if err := binary.Read(bytes.NewReader(ico), binary.LittleEndian, &head); err != nil || head.Type != 1 || int(head.Count) != len(icoSizes) {
		t.Fatalf("the .ico header %+v (%v)", head, err)
	}
	for i, n := range icoSizes {
		e := ico[6+16*i:]
		size := binary.LittleEndian.Uint32(e[8:])
		off := binary.LittleEndian.Uint32(e[12:])
		if w := int(e[0]); w != n%256 || !bytes.HasPrefix(ico[off:off+size], []byte("\x89PNG")) {
			t.Errorf("entry %d: width byte %d, want %d", i, w, n%256)
		}
	}
	got, err := os.ReadFile(filepath.Join(out, "icon-256.png"))
	if err != nil {
		t.Fatal(err)
	}
	want, err := os.ReadFile("../../internal/gamecfg/icon.png")
	if err != nil {
		t.Fatal(err)
	}
	// The committed render came from an arm64 host; amd64's floating
	// point (no fused multiply-add in the same places) moves some edge
	// pixels by one step, so the embed is compared by pixels, not bytes.
	if d := maxDelta(t, got, want); d > 2 {
		t.Errorf("internal/gamecfg/icon.png is not the SVG's 256 render (a channel differs by %d): run go run ./cmd/icongen -in icon/wizard-icon.svg -size 256 > internal/gamecfg/icon.png", d)
	}
	var one bytes.Buffer
	if err := run([]string{"-in", "../../icon/wizard-icon.svg", "-size", "256"}, &one); err != nil || !bytes.Equal(one.Bytes(), got) {
		t.Errorf("-size 256 differs from -out's (%v)", err)
	}
	// The same SVG behind a UTF-8 byte order mark renders the same.
	svg, err := os.ReadFile("../../icon/wizard-icon.svg")
	if err != nil {
		t.Fatal(err)
	}
	bom := filepath.Join(t.TempDir(), "bom.svg")
	if err := os.WriteFile(bom, append([]byte("\xef\xbb\xbf"), svg...), 0o644); err != nil {
		t.Fatal(err)
	}
	var withBOM bytes.Buffer
	if err := run([]string{"-in", bom, "-size", "256"}, &withBOM); err != nil || !bytes.Equal(withBOM.Bytes(), got) {
		t.Errorf("an SVG with a BOM: %v", err)
	}
	if err := run([]string{"-in", "main.go", "-size", "8"}, &one); err == nil {
		t.Error("a Go file is not an icon")
	}
}

// maxDelta is the largest difference of one channel between two PNGs of
// one size.
func maxDelta(t *testing.T, a, b []byte) int {
	t.Helper()
	ia, err := png.Decode(bytes.NewReader(a))
	if err != nil {
		t.Fatal(err)
	}
	ib, err := png.Decode(bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	if ia.Bounds() != ib.Bounds() {
		t.Fatalf("sizes %v and %v", ia.Bounds(), ib.Bounds())
	}
	d := 0
	r := ia.Bounds()
	for y := r.Min.Y; y < r.Max.Y; y++ {
		for x := r.Min.X; x < r.Max.X; x++ {
			ca := color.NRGBAModel.Convert(ia.At(x, y)).(color.NRGBA)
			cb := color.NRGBAModel.Convert(ib.At(x, y)).(color.NRGBA)
			for _, p := range [][2]uint8{{ca.R, cb.R}, {ca.G, cb.G}, {ca.B, cb.B}, {ca.A, cb.A}} {
				d = max(d, abs(int(p[0])-int(p[1])))
			}
		}
	}
	return d
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}
