#!/usr/bin/env bash
# The Go module's gates (docs/wizard.md): gofmt, vet, the tests, and
# staticcheck when it is installed. `xbr wizard check` runs this with the
# pinned Go first on PATH and GOTOOLCHAIN=local, then the double build.
set -euo pipefail
cd "$(dirname "$0")"
test "$(go env GOTOOLCHAIN)" = local || { echo "check.sh: GOTOOLCHAIN must be local" >&2; exit 1; }
echo "go: $(go env GOVERSION) ($(go env GOROOT))"
# .tools/ holds the pinned Go and the caches: not ours to format.
unformatted=$(find . -name '*.go' -not -path './.tools/*' -print0 | xargs -0 gofmt -l)
if [ -n "$unformatted" ]; then
	echo "gofmt: these files need formatting:" >&2
	echo "$unformatted" >&2
	exit 1
fi
echo "gofmt: ok"
go vet ./...
echo "vet: ok"
go test -count=1 ./...
sc=$(command -v staticcheck || true)
[ -n "$sc" ] || [ ! -x "$(go env GOPATH)/bin/staticcheck" ] || sc="$(go env GOPATH)/bin/staticcheck"
if [ -n "$sc" ]; then
	"$sc" ./...
	echo "staticcheck: ok"
else
	echo "staticcheck: not installed, skipped (GOFLAGS= go install honnef.co/go/tools/cmd/staticcheck@v0.8.2 puts it in .tools/)"
fi
