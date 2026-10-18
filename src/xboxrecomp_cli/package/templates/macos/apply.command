#!/bin/sh
# Applies a macOS patch: the installed app becomes the new version, with
# only the changed files read from this patch.
#
#   apply.command [--app PATH] [--into DIR] [--yes]
#   apply.command rollback [--app PATH]
#
# Double-click it in the patch's disk image, or run it in Terminal. The
# app is found at /Applications/<App>.app, then ~/Applications/<App>.app
# (--app names another). --into DIR puts the new version in DIR instead
# (offered when the app's folder cannot be written).
#
# How (so a power loss or a kill never leaves the path without an app):
#   1. the installed app must be the version this patch is for, with the
#      same manifest, and every file the patch keeps must match it;
#   2. the app is cloned beside itself (.<App>.app.partial: an APFS clone
#      shares every block, so nothing is copied);
#   3. the patch is laid over the clone, which is checked against the new
#      SHA256SUMS and by codesign;
#   4. xbr-swap flushes the clone and swaps it with the app in one call;
#   5. the old app stays as .<App>.previous.app, with this script and
#      xbr-swap in .<App>.previous.tools/, so rollback works after the
#      patch is deleted. rollback swaps the two apps back.
# Running it again after a stop finishes the run. Your saves and settings
# (~/Library/Application Support/<App>/) are never written.
#
# Standard macOS tools only (shasum, cp, ditto, codesign, PlistBuddy).
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
PATCH="$HERE/patch"
SWAP=${XBR_SWAP:-"$HERE/xbr-swap"}

say() { printf '%s\n' "$*"; }
die() { printf 'apply: %s\n' "$*" >&2; exit 1; }

# A test stands in for a kill at a named step.
stop_at() {
	if [ "${XBR_APPLY_STOP:-}" = "$1" ]; then
		kill -9 $$
	fi
}

sha_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }

plist_version() {
	/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$1/Contents/Info.plist" 2>/dev/null ||
		echo "unknown"
}

CMD=apply
APP_PATH=""
INTO=""
YES=0
while [ $# -gt 0 ]; do
	case "$1" in
	rollback) CMD=rollback ;;
	--app) APP_PATH=${2:-}; shift ;;
	--into) INTO=${2:-}; shift ;;
	--yes) YES=1 ;;
	-h | --help) sed -n '2,12p' "$0"; exit 0 ;;
	*) die "unknown argument: $1 (see --help)" ;;
	esac
	shift
done

# The tools copy beside a previous app knows its app by its own name.
case "$(basename "$HERE")" in
.*.previous.tools)
	APP=$(basename "$HERE" | sed -e 's/^\.//' -e 's/\.previous\.tools$//')
	[ -n "$APP_PATH" ] || APP_PATH="$(dirname "$HERE")/$APP.app"
	;;
*)
	[ -f "$PATCH/patch.env" ] || die "no patch here ($PATCH/patch.env is missing)"
	# shellcheck disable=SC1091
	. "$PATCH/patch.env"
	;;
esac

if [ -z "$APP_PATH" ]; then
	for d in /Applications "$HOME/Applications"; do
		if [ -d "$d/$APP.app" ]; then
			APP_PATH="$d/$APP.app"
			break
		fi
	done
fi
if [ -z "$APP_PATH" ] || [ ! -d "$APP_PATH" ]; then
	# A patch refuses in D10's words; a rollback has no base to name.
	[ -z "${BASE_VERSION:-}" ] || die "This update is for $BASE_VERSION; this install is not installed (no $APP.app in /Applications or ~/Applications). Use the full installer for $VERSION."
	die "$APP.app is not installed (in /Applications or ~/Applications); use the full disk image"
fi
APP_PATH=$(cd "$APP_PATH" && pwd)
DIR=$(dirname "$APP_PATH")
PREV="$DIR/.$APP.previous.app"
TOOLS="$DIR/.$APP.previous.tools"

if pgrep -f "$APP_PATH/Contents/MacOS/" >/dev/null 2>&1; then
	die "$APP is running; quit it first"
fi

swap() {
	"$SWAP" swap "$1" "$2"
	rc=$?
	case $rc in
	0) return 0 ;;
	3)
		say "apply: macOS refused to replace $APP_PATH (Operation not permitted)." >&2
		say "  Give Terminal (or the build wizard) the App Management permission:" >&2
		say "  System Settings > Privacy & Security > App Management, then run this again." >&2
		return 3
		;;
	4)
		say "apply: $DIR is not writable by $(id -un)." >&2
		return 4
		;;
	*) return 1 ;;
	esac
}

offer_home() {
	# Nothing of the app has changed; the new version can go to the
	# user's own Applications folder instead.
	alt="$HOME/Applications"
	[ "$DIR" != "$alt" ] || exit 1
	if [ -t 0 ] && [ "$YES" = 0 ]; then
		printf 'Install the new version into %s instead? [y/N] ' "$alt"
		read -r ans || ans=""
		case "$ans" in
		y | Y | yes) exec "$0" --app "$APP_PATH" --into "$alt" ;;
		esac
	fi
	say "  Or install it into $alt: $0 --app \"$APP_PATH\" --into \"$alt\"" >&2
	exit 1
}

if [ "$CMD" = rollback ]; then
	[ -d "$PREV" ] || die "no previous version of $APP beside $APP_PATH"
	from=$(plist_version "$APP_PATH")
	to=$(plist_version "$PREV")
	swap "$PREV" "$APP_PATH" || exit 1
	say "rolled back: $from -> $to (the newer one is kept as $PREV)"
	exit 0
fi

J="$DIR/.$APP.apply.json"
PARTIAL="$DIR/.$APP.app.partial"
[ -z "$INTO" ] || PARTIAL="$INTO/.$APP.app.partial"

journal() {
	printf '{"version": "%s", "base": "%s", "step": "%s"}\n' "$VERSION" "$BASE_VERSION" "$1" >"$J.tmp" &&
		mv -f "$J.tmp" "$J"
}

keep_previous() {
	# The base app (now at .partial) becomes the previous one, with the
	# tools to roll back to it.
	rm -rf "$PREV" "$TOOLS"
	mv "$PARTIAL" "$PREV" || die "cannot keep the previous app at $PREV"
	mkdir -p "$TOOLS"
	cp "$HERE/apply.command" "$TOOLS/apply.command"
	cp "$SWAP" "$TOOLS/xbr-swap"
	chmod 755 "$TOOLS/apply.command" "$TOOLS/xbr-swap"
	"$SWAP" sync "$TOOLS" >/dev/null 2>&1 || true
	rm -f "$J"
}

cur_sha=$(sha_of "$APP_PATH/Contents/Resources/manifest.json")

# A rerun after the swap: the new version is in place.
if [ "$cur_sha" = "$NEW_SHA" ]; then
	if [ -d "$PARTIAL" ] && [ "$(sha_of "$PARTIAL/Contents/Resources/manifest.json")" = "$BASE_SHA" ]; then
		keep_previous
	else
		rm -rf "$PARTIAL"
		rm -f "$J"
	fi
	say "$APP $VERSION is installed at $APP_PATH"
	exit 0
fi

if [ "$cur_sha" != "$BASE_SHA" ]; then
	found=$(plist_version "$APP_PATH")
	[ "$found" != "$BASE_VERSION" ] || found="$found with another manifest"
	die "This update is for $BASE_VERSION; this install is $found. Use the full installer for $VERSION."
fi

# Every name the lists give, before anything is written: they become
# rm -rf and rm -f arguments, so each must stay inside the app.
safe_rel() {
	case "$1" in
	"" | /* | ../* | */../* | */.. | .. | ./* | */./* | */. | . | *//*) return 1 ;;
	esac
	return 0
}
for list in whole.list deleted.list kept.list; do
	[ -f "$PATCH/$list" ] || die "the patch has no $list (a damaged patch?)"
	while IFS= read -r line || [ -n "$line" ]; do
		rel=$line
		[ "$list" != kept.list ] || rel=${line#* * }
		[ "$list" != kept.list ] || [ "$rel" != "$line" ] ||
			die "damaged patch ($list: \"$line\"); nothing was changed"
		safe_rel "$rel" || die "damaged patch ($list: unsafe path \"$rel\"); nothing was changed"
	done <"$PATCH/$list"
done

# Every kept file, by size then sha256, before anything is written.
say "checking $APP_PATH ..."
RES="$APP_PATH/Contents/Resources"
sums=$(mktemp -t xbr-kept) || die "no temporary file"
trap 'rm -f "$sums"' EXIT
while IFS=' ' read -r size sha path; do
	[ -n "$path" ] || continue
	got=$(stat -f %z "$RES/$path" 2>/dev/null || echo -1)
	if [ "$got" != "$size" ]; then
		die "This update is for $BASE_VERSION, but $path does not match it. Use the full installer for $VERSION."
	fi
	printf '%s  %s\n' "$sha" "$path" >>"$sums"
done <"$PATCH/kept.list"
if [ -s "$sums" ]; then
	bad=$(cd "$RES" && shasum -a 256 -c "$sums" 2>/dev/null | grep -v ': OK$' | head -1 | sed 's/: FAILED.*//')
	if [ -n "$bad" ]; then
		die "This update is for $BASE_VERSION, but $bad does not match it. Use the full installer for $VERSION."
	fi
fi

PDIR=$(dirname "$PARTIAL")
if [ -n "$INTO" ]; then
	mkdir -p "$INTO" || die "cannot make $INTO"
	[ ! -e "$INTO/$APP.app" ] || die "$INTO/$APP.app is there already"
fi
if [ ! -w "$PDIR" ]; then
	say "apply: $PDIR is not writable by $(id -un); nothing was changed." >&2
	offer_home
fi

journal clone
rm -rf "$PARTIAL"
# An APFS clone where the volume has them, a copy elsewhere.
if ! cp -c -R "$APP_PATH" "$PARTIAL" 2>/dev/null; then
	rm -rf "$PARTIAL"
	if ! cp -R "$APP_PATH" "$PARTIAL"; then
		rm -rf "$PARTIAL"
		die "cannot copy the app to $PARTIAL; nothing was changed"
	fi
fi
stop_at cloned

journal overlay
C="$PARTIAL/Contents"
while read -r part; do
	[ -n "$part" ] || continue
	rm -rf "${C:?}/$part"
	if [ -e "$PATCH/Contents/$part" ]; then
		ditto "$PATCH/Contents/$part" "$C/$part" || die "cannot write $C/$part"
	fi
done <"$PATCH/whole.list"
if [ -d "$PATCH/Contents/Resources" ]; then
	ditto "$PATCH/Contents/Resources" "$C/Resources" || die "cannot write $C/Resources"
fi
while read -r path; do
	[ -n "$path" ] || continue
	rm -f "$C/Resources/$path"
done <"$PATCH/deleted.list"
stop_at overlaid

journal verify
if ! (cd "$C/Resources" && shasum -a 256 -c -s SHA256SUMS); then
	rm -rf "$PARTIAL"
	die "the patched app does not match its SHA256SUMS; nothing was changed"
fi
if ! codesign --verify --deep --strict "$PARTIAL" 2>/dev/null; then
	rm -rf "$PARTIAL"
	die "the patched app's signature does not verify; nothing was changed"
fi
stop_at verified

if [ -n "$INTO" ]; then
	"$SWAP" sync "$PARTIAL" || { rm -rf "$PARTIAL"; die "cannot flush $PARTIAL"; }
	mv "$PARTIAL" "$INTO/$APP.app" || die "cannot move the app into $INTO"
	"$SWAP" sync "$INTO" >/dev/null 2>&1 || true
	rm -f "$J"
	say "$APP $VERSION is installed at $INTO/$APP.app ($APP_PATH is still the old version)"
	exit 0
fi

journal swap
swap "$PARTIAL" "$APP_PATH"
rc=$?
if [ $rc -ne 0 ]; then
	rm -rf "$PARTIAL"
	rm -f "$J"
	say "  nothing was changed" >&2
	[ $rc -eq 1 ] && exit 1
	offer_home
fi
stop_at swapped

journal previous
keep_previous
say "$APP $BASE_VERSION -> $VERSION at $APP_PATH"
say "rollback: $TOOLS/apply.command rollback"
