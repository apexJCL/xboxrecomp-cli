// Package update is the release check: one request at start for the game
// repo's latest release, a notice when its tag is newer than the embedded
// one. Never an error and never a download (docs/wizard.md).
package update

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Timeout bounds the whole check.
const Timeout = 3 * time.Second

type Release struct {
	Tag  string `json:"tag_name"`
	Body string `json:"body"`
	URL  string `json:"html_url"`
}

var repoRE = regexp.MustCompile(`^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$`)

// APIURL is the latest-release endpoint for a GitHub repo URL, "" for any
// other host.
func APIURL(repo string) string {
	m := repoRE.FindStringSubmatch(repo)
	if m == nil {
		return ""
	}
	return fmt.Sprintf("https://api.github.com/repos/%s/%s/releases/latest", m[1], m[2])
}

var numRE = regexp.MustCompile(`\d+`)

// Newer reports whether tag a is a later release than b: their numbers
// compared in order (v0.10.0 > v0.9.3), so the game repo's own scheme
// (vX.Y.Z, X.Y) works without a semver parser.
func Newer(a, b string) bool {
	na, nb := numRE.FindAllString(a, -1), numRE.FindAllString(b, -1)
	if len(na) == 0 || len(nb) == 0 {
		return false
	}
	for i := 0; i < max(len(na), len(nb)); i++ {
		var x, y int
		if i < len(na) {
			x, _ = strconv.Atoi(na[i])
		}
		if i < len(nb) {
			y, _ = strconv.Atoi(nb[i])
		}
		if x != y {
			return x > y
		}
	}
	return false
}

// Latest is the newer release, or nil: not newer, offline, a timeout, a
// rate limit or a reply it cannot read. why says which, for the log.
func Latest(ctx context.Context, client *http.Client, repo, current string) (rel *Release, why string) {
	url := APIURL(repo)
	if url == "" || current == "" {
		return nil, "no GitHub repo or no release tag to compare"
	}
	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err.Error()
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err.Error()
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, "HTTP " + resp.Status
	}
	var r Release
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, "bad reply: " + err.Error()
	}
	if !Newer(r.Tag, current) {
		return nil, fmt.Sprintf("latest is %s, this is %s", r.Tag, current)
	}
	// The reply's page is opened in the browser: only a GitHub page is
	// taken from it, anything else falls back to the release URL built
	// from the repo the binary was made for.
	if !strings.HasPrefix(r.URL, "https://github.com/") {
		r.URL = strings.TrimSuffix(strings.TrimSuffix(repo, "/"), ".git") + "/releases/tag/" + r.Tag
	}
	return &r, "newer: " + r.Tag
}
