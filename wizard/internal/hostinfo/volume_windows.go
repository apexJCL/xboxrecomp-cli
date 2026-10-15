//go:build windows

package hostinfo

import (
	"path/filepath"
	"strings"
)

// sameVolume compares the drive letters (or UNC shares) of two existing
// paths.
func sameVolume(a, b string) bool {
	va, vb := filepath.VolumeName(a), filepath.VolumeName(b)
	return va != "" && strings.EqualFold(va, vb)
}
