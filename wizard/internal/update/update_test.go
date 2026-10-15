package update

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type stub func(*http.Request) (*http.Response, error)

func (s stub) RoundTrip(r *http.Request) (*http.Response, error) { return s(r) }

func reply(code int, body string) *http.Client {
	return &http.Client{Transport: stub(func(r *http.Request) (*http.Response, error) {
		if r.URL.String() != "https://api.github.com/repos/o/g/releases/latest" {
			return nil, io.ErrUnexpectedEOF
		}
		return &http.Response{StatusCode: code, Status: http.StatusText(code), Body: io.NopCloser(strings.NewReader(body))}, nil
	})}
}

func TestLatest(t *testing.T) {
	const repo = "https://github.com/o/g.git"
	ctx := context.Background()
	cases := []struct {
		name   string
		client *http.Client
		newer  bool
	}{
		{"newer", reply(200, `{"tag_name":"v0.10.0","body":"notes"}`), true},
		{"github page", reply(200, `{"tag_name":"v0.10.0","body":"notes","html_url":"https://github.com/o/g/releases/tag/v0.10.0"}`), true},
		{"foreign page", reply(200, `{"tag_name":"v0.10.0","body":"notes","html_url":"https://example.com/phish"}`), true},
		{"file page", reply(200, `{"tag_name":"v0.10.0","body":"notes","html_url":"file:///etc/passwd"}`), true},
		{"same", reply(200, `{"tag_name":"v0.9.3"}`), false},
		{"older", reply(200, `{"tag_name":"v0.9.2"}`), false},
		{"403", reply(403, `{"message":"rate limit"}`), false},
		{"bad json", reply(200, `{"tag_name":`), false},
		{"timeout", &http.Client{Transport: stub(func(r *http.Request) (*http.Response, error) {
			<-r.Context().Done()
			return nil, r.Context().Err()
		})}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			start := time.Now()
			rel, why := Latest(ctx, tc.client, repo, "v0.9.3")
			if (rel != nil) != tc.newer || why == "" {
				t.Fatalf("%v %q", rel, why)
			}
			if rel != nil && (rel.Body != "notes" || rel.URL != "https://github.com/o/g/releases/tag/v0.10.0") {
				t.Fatalf("%+v", rel)
			}
			if time.Since(start) > Timeout+time.Second {
				t.Fatal("the check outlived its timeout")
			}
		})
	}
}

func TestNewer(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{{"v1.0", "v0.9", true}, {"v0.10.0", "v0.9.9", true}, {"v1", "v1.0.0", false}, {"v1.0.1", "v1", true}, {"x", "v1", false}} {
		if Newer(c.a, c.b) != c.want {
			t.Errorf("Newer(%s, %s)", c.a, c.b)
		}
	}
	if APIURL("https://gitlab.com/o/g") != "" {
		t.Error("not GitHub")
	}
}
