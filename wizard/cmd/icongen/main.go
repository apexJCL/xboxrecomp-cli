// icongen renders the wizard's icon set from one SVG or PNG: the PNG
// sizes, a Windows .ico and the 1024-pixel Icon.png that `fyne package`
// turns into the macOS .icns (docs/wizard.md, The icon). xbr wizard build
// runs it; nothing it writes is committed but internal/gamecfg/icon.png.
//
//	icongen -in FILE -out DIR    every size, wizard.ico and Icon.png
//	icongen -in FILE -size N     one N-pixel PNG on stdout
//
// The rendering is Fyne's own (the software painter the screenshot tests
// use) at 1024 pixels, then an area average down to each size in integer
// arithmetic: pure Go, and the same bytes for the same input and Fyne
// version, which the double build checks. Fyne's SVG rasterizer keeps a
// stroke's width in pixels whatever the scale, so an SVG drawn for 1024
// is only right when rendered at 1024.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"os"
	"path/filepath"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/driver/software"
	"fyne.io/fyne/v2/test"
	"fyne.io/fyne/v2/theme"
)

// Sizes are the PNGs written; icoSizes the .ico's entries (the shell's
// small, list and large views, and the 256 one for the big views).
var (
	sizes    = []int{16, 24, 32, 48, 64, 128, 256, 512, 1024}
	icoSizes = []int{16, 24, 32, 48, 256}
)

// clear is a theme whose background is transparent: the canvas paints
// its background first, and an icon's corners must stay see-through.
type clear struct{ fyne.Theme }

func (c clear) Color(n fyne.ThemeColorName, v fyne.ThemeVariant) color.Color {
	if n == theme.ColorNameBackground {
		return color.Transparent
	}
	return c.Theme.Color(n, v)
}

// resource is the input as Fyne reads it: an SVG by its first tag, else a
// PNG by its signature. An SVG may start with a UTF-8 byte order mark, as
// xbr's own check allows; Fyne's parser gets it without one.
func resource(b []byte) (fyne.Resource, error) {
	if svg := bytes.TrimPrefix(b, []byte("\xef\xbb\xbf")); bytes.HasPrefix(bytes.TrimSpace(svg), []byte("<svg")) ||
		bytes.HasPrefix(bytes.TrimSpace(svg), []byte("<?xml")) {
		return fyne.NewStaticResource("icon.svg", svg), nil
	}
	if bytes.HasPrefix(b, []byte("\x89PNG\r\n\x1a\n")) {
		return fyne.NewStaticResource("icon.png", b), nil
	}
	return nil, errors.New("not a PNG or SVG")
}

// master is the size everything is rendered at.
const master = 1024

// renderMaster is the icon at master by master pixels.
func renderMaster(res fyne.Resource) (image.Image, error) {
	img := canvas.NewImageFromResource(res)
	img.FillMode = canvas.ImageFillContain
	img.ScaleMode = canvas.ImageScaleSmooth
	img.SetMinSize(fyne.NewSquareSize(master))
	//lint:ignore SA1019 a fixed variant is what an image needs
	out := software.Render(img, clear{theme.LightTheme()})
	if b := out.Bounds(); b.Dx() != master || b.Dy() != master {
		return nil, fmt.Errorf("rendered %v, not %d by %d", b, master, master)
	}
	return out, nil
}

// shrink is src (square) at n by n: each pixel the average of the source
// area it covers, weighted by the overlap and in premultiplied colour, so
// a transparent corner does not darken the edge next to it. Integers only:
// the same bytes on every machine.
func shrink(src image.Image, n int) *image.RGBA {
	b := src.Bounds()
	m := b.Dx()
	// In units of 1/(m*n) of the side, source pixel i spans [i*n, (i+1)*n)
	// and output pixel o spans [o*m, (o+1)*m).
	overlap := func(o, i int) uint64 {
		lo, hi := max(o*m, i*n), min((o+1)*m, (i+1)*n)
		return uint64(max(hi-lo, 0))
	}
	out := image.NewRGBA(image.Rect(0, 0, n, n))
	total := uint64(m) * uint64(m)
	for oy := 0; oy < n; oy++ {
		y0, y1 := oy*m/n, ((oy+1)*m+n-1)/n
		for ox := 0; ox < n; ox++ {
			x0, x1 := ox*m/n, ((ox+1)*m+n-1)/n
			var sr, sg, sb, sa uint64
			for y := y0; y < y1; y++ {
				wy := overlap(oy, y)
				for x := x0; x < x1; x++ {
					w := wy * overlap(ox, x)
					r, g, bl, a := src.At(b.Min.X+x, b.Min.Y+y).RGBA()
					sr += w * uint64(r)
					sg += w * uint64(g)
					sb += w * uint64(bl)
					sa += w * uint64(a)
				}
			}
			px := func(v uint64) uint8 { return uint8(((v+total/2)/total*255 + 32767) / 65535) }
			out.SetRGBA(ox, oy, color.RGBA{R: px(sr), G: px(sg), B: px(sb), A: px(sa)})
		}
	}
	return out
}

// render is the icon at n by n pixels as a PNG.
func render(src image.Image, n int) ([]byte, error) {
	var img image.Image = src
	if n != src.Bounds().Dx() {
		img = shrink(src, n)
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

// ico is a .ico of PNG entries: the 6-byte header, a 16-byte directory
// entry per image, then the PNGs. Explorer has read PNG entries since
// Vista.
func ico(pngs map[int][]byte, order []int) []byte {
	var b bytes.Buffer
	le := func(v any) { _ = binary.Write(&b, binary.LittleEndian, v) }
	le(uint16(0))
	le(uint16(1)) // an icon, not a cursor
	le(uint16(len(order)))
	off := 6 + 16*len(order)
	for _, n := range order {
		side := uint8(n)
		if n >= 256 {
			side = 0 // 0 is 256
		}
		le([4]uint8{side, side, 0, 0}) // width, height, no palette, reserved
		le(uint16(1))                  // planes
		le(uint16(32))                 // bits per pixel
		le(uint32(len(pngs[n])))
		le(uint32(off))
		off += len(pngs[n])
	}
	for _, n := range order {
		b.Write(pngs[n])
	}
	return b.Bytes()
}

func run(args []string, stdout io.Writer) error {
	fs := flag.NewFlagSet("icongen", flag.ContinueOnError)
	in := fs.String("in", "", "the icon: an SVG or a PNG (512 pixels or more)")
	out := fs.String("out", "", "write every size, wizard.ico and Icon.png here")
	size := fs.Int("size", 0, "write one PNG of this size to stdout")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *in == "" || (*out == "") == (*size == 0) {
		return errors.New("usage: icongen -in FILE (-out DIR | -size N)")
	}
	b, err := os.ReadFile(*in)
	if err != nil {
		return err
	}
	res, err := resource(b)
	if err != nil {
		return fmt.Errorf("%s: %w", *in, err)
	}
	a := test.NewApp()
	defer a.Quit()
	src, err := renderMaster(res)
	if err != nil {
		return err
	}
	if *size > 0 {
		p, err := render(src, *size)
		if err != nil {
			return err
		}
		_, err = stdout.Write(p)
		return err
	}
	if err := os.MkdirAll(*out, 0o755); err != nil {
		return err
	}
	pngs := map[int][]byte{}
	for _, n := range sizes {
		if pngs[n], err = render(src, n); err != nil {
			return err
		}
		if err := os.WriteFile(filepath.Join(*out, fmt.Sprintf("icon-%d.png", n)), pngs[n], 0o644); err != nil {
			return err
		}
	}
	if err := os.WriteFile(filepath.Join(*out, "wizard.ico"), ico(pngs, icoSizes), 0o644); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(*out, "Icon.png"), pngs[1024], 0o644)
}

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "icongen:", err)
		os.Exit(1)
	}
}
