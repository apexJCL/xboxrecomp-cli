package screens

import (
	"fmt"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"

	"wizard/internal/hostinfo"
)

// Every sentence a player reads, in one place. The player has no build
// knowledge: no tool names, no commands, no CLI words here (TestCopy
// checks); the CLI's own lines stay in the rows' details and the log
// pane, where someone helping the player can ask for them. The game is
// named only through the config.

// The frame.
const (
	stepText   = "Step %d of %d"
	footerText = "Build wizard %s"
	footerDev  = "Build wizard %s (developer build)"
	devRelease = "developer build of the working tree"
	// The local-source banner (D14), on every screen and in --dry.
	localBanner      = "Local source: %s, not a release; nothing is published"
	localBannerDirty = "uncommitted changes in %s are not built"
	techPins         = "Wizard %s; release %s (%s); xboxrecomp-cli %s; toolkit %s"
	nextText         = "Next"
	backText         = "Back"
)

// Welcome.
const (
	titleWelcome    = "Welcome"
	noGameLead      = "No game inside."
	downloadsLead   = "Downloads only what it needs."
	downloadsText   = "The free build tools and the game's source code come from their official sites, and each download is checked against a known checksum."
	sendsLead       = "Sends nothing of yours."
	buildFolder     = "Build folder"
	buildFolderIs   = "The tools, the source code and the build go here. About %d GB."
	outputFolder    = "Output folder"
	outputFolderIs  = "The finished game bundle goes here. About %d GB."
	changeText      = "Change…"
	updateCheckText = "Check for a newer wizard"
	unsignedText    = "This program is not signed; the release page lists each download's SHA256."
	newerKeepsText  = "This one keeps working. Download the new one from the release page if you like."
	openReleaseText = "Open the release page"
	closeText       = "Close"
	pickFolderTitle = "Build folder"
	pickOutTitle    = "Output folder"
)

// The open-source credits: on Welcome and in the header's info dialog.
const (
	creditsHeading = "Made with these open-source projects"
	creditsTitle   = "Open-source credits"
	creditsNotices = "NOTICES.txt, which came with the wizard, holds each licence's full text."
	// What the project does for the build, then its licence.
	creditMeta = "%s · %s"
)

func welcomeHeading(game string) string { return fmt.Sprintf("Build %s for your own computer", game) }

// welcomeIntro names the computers the config's targets build for; the
// build time is the game's, so it is not promised here.
func welcomeIntro(game string, targets []string) string {
	// The Steam Deck item goes last and the list takes a serial comma, so
	// "such as the Steam Deck" cannot read as joined to the next item.
	ordered := slices.Clone(targets)
	slices.SortStableFunc(ordered, func(a, b string) int { return targetRank(a) - targetRank(b) })
	var on []string
	for _, t := range ordered {
		if c, ok := targetComputer[t]; ok {
			on = append(on, c)
		} else {
			on = append(on, t)
		}
	}
	list := strings.Join(on, ", ")
	switch n := len(on); {
	case n == 2:
		list = on[0] + " or " + on[1]
	case n > 2:
		list = strings.Join(on[:n-1], ", ") + ", or " + on[n-1]
	}
	return fmt.Sprintf("This wizard turns your own copy of %s into a version that runs on %s. "+
		"It asks a few questions, then the build runs on its own; this can take a while.", game, list)
}

func targetRank(t string) int {
	if t == "steamos" {
		return 1
	}
	return 0
}

// lowerFirst lowers a sentence's first letter, for use after a colon.
func lowerFirst(s string) string {
	r, n := utf8.DecodeRuneInString(s)
	if n == 0 {
		return s
	}
	return string(unicode.ToLower(r)) + s[n:]
}

// targetComputer is a target as the computer it runs on, for a sentence.
var targetComputer = map[string]string{
	"windows": "a Windows PC",
	"macos":   "a Mac",
	"steamos": "a Linux PC such as the Steam Deck",
}

func noGameText(game string) string {
	return fmt.Sprintf("This program holds no code or files from %s. The game comes from your own disc image or game folder, "+
		"and what it builds is for your own computers only.", game)
}

// sendsText is true of the code: update.Latest is one GET with no body
// and no identifying header; every other request is a download by
// checksum or the clone.
// The first check waits until the player leaves the Welcome page, so the
// box below is seen before anything goes out.
const sendsText = "Your game, your files and your settings stay on this computer. " +
	"Apart from the downloads above, the only other request is a check for a newer wizard, which you can turn off below."

func newerTitle(tag string) string { return "A newer wizard is available: " + tag }

// Checks.
const (
	titleChecks     = "Checks"
	retryText       = "Retry"
	detailsText     = "Details"
	hideDetailsText = "Hide details"
	copyText        = "Copy"
	checksDone      = "Checks finished."
	checksStopped   = "The checks stopped: %s"

	rowTools   = "Build tools"
	rowSource  = "Game source"
	rowNotes   = "Notes"
	rowPrepare = "Getting ready"

	statusChecking  = "Checking…"
	statusReady     = "Ready"
	statusFetched   = "Fetched when the build starts"
	statusNotPinned = "Not at the release's version"
	statusDevMode   = "Not at the release's version (developer mode)"
	statusLocal     = "A local source, not a release"
	statusBuildable = "Can be built on this computer"
	statusNeeds     = "Needs something first"
	statusNotes     = "Worth knowing"
	statusFailed    = "Could not finish"

	toolsFetched = "The build fetches these itself: %s."
	toolsAllHere = "Every build tool is in place."
	sourceAt     = "%s %s in %s"
	sourceDev    = "%s, as it is (developer mode)"
	sourceLocal  = "%s at %s, cloned into %s (local source)"

	installCLT      = "Install Apple's Command Line Tools"
	installHomebrew = "Install Homebrew"
	installBrew     = "Install: %s"
	runInTerminal   = "Run in a terminal: %s"
	cltOpened       = "Apple's installer is open; this page checks again when it finishes."
	homebrewOpened  = "Homebrew's installer is open in Terminal; this page checks again when it finishes."

	blockCLT        = "Apple's Command Line Tools are not installed. They bring git and the compiler. The button opens Apple's installer; it takes a few minutes, and this page checks again when it is done."
	blockBrewHave   = "Some libraries from Homebrew are missing: %s. The button installs them; no password is needed."
	blockBrewNone   = "The build needs a few libraries from Homebrew, which is not installed. The button opens Homebrew's installer in Terminal, where it asks for your password."
	blockNSIS       = "Making a Windows setup program needs NSIS (makensis)."
	blockNSISBrew   = "The button installs it."
	blockNSISNoBrew = "It comes from Homebrew, which is not installed yet: the button opens Homebrew's installer in Terminal, where it asks for your password. Then this page offers NSIS."
	blockNSISSudo   = "Run this in a terminal (it asks for your password there): %s"
	blockNSISOther  = "On this system it runs in a container: %s"
	blockMacOnly    = "A Mac app can only be built on a Mac. Pick another platform, or run this wizard on a Mac."
	prepareOverlap  = "The build folder %s and the game's saves folder %s overlap. Pick another build folder on the Welcome page, so your saves stay apart from the build."
	prepareNoSpace  = "The build folder's disk has %.1f GB free and this needs about %.1f GB. Free some space, or pick another build folder on the Welcome page."
	gitMissingMac   = "git comes with Apple's Command Line Tools, which this Mac does not have yet."
	gitMissingOther = "This system has no git, which the build needs. Install it with: %s"
)

// targetRowName names a platform row of the checks table.
var targetRowName = map[string]string{
	"windows": "Build for Windows",
	"macos":   "Build for macOS",
	"steamos": "Build for Steam Deck or Linux",
}

// Your game.
const (
	titleDump       = "Your game"
	chooseImage     = "Choose a disc image…"
	chooseFolder    = "Choose a folder…"
	dumpPlaceholder = "or type the path here"
	dumpNothing     = "Nothing was found at %s."
	dumpNoXBE       = "%s has no default.xbe. Pick the game's top folder, or the disc image."
	disksTitle      = "Disc images"
	dumpNoSpaceOut  = "The output folder's disk has %.1f GB free and the bundle needs about %.1f GB. Free some space, or pick another output folder on the Welcome page."
)

// outClashText says why an output folder is refused, by hostinfo.OutClash.
var outClashText = map[string]string{
	hostinfo.ClashData: "The output folder must not be, hold or sit inside the game's data folder, where your saves and settings live. Pick another one.",
	hostinfo.ClashSrc:  "The output folder must not be, hold or sit inside the build folder's src folder, which a rebuild rewrites. Pick another one.",
	hostinfo.ClashDump: "The output folder must not be, hold or sit inside your game's folder. Pick another output folder on the Welcome page.",
}

func dumpIntro(game string) string {
	return fmt.Sprintf("Pick your own copy of %s: a disc image (.iso or .xiso), or the folder that holds default.xbe. "+
		"The wizard checks that it is the right game and copies the files into its own folder; your original is not changed.", game)
}

func dumpDialogTitle(game string) string { return "Your copy of " + game }

// Platform.
const (
	titleTarget   = "Platform"
	targetIntro   = "Which computer is the game for? This one is already selected. A build for another computer of yours is copied there and installed the same way."
	targetBlocked = "%s: not on this computer yet. The Checks page (step 2) says what it needs."
	targetDeck    = "On a Steam Deck the build is slow and needs a lot of memory; a PC or Mac is faster."
)

// Options.
const (
	titleOptions    = "Options"
	optionsIntro    = "The game starts with these. Most players leave them as they are."
	optionsNone     = "This game has no settings to choose here."
	optionsLater    = "Later, change these and more in config.toml: %s"
	optionsNoConfig = "This version of the game has no settings file to change later."
	optionLabelled  = "%s (%s)"
	optionFontFound = "%s is installed."
)

// Summary.
const (
	titleSummary     = "Summary"
	summaryNext      = "Build"
	summaryGame      = "Game: %s %s"
	summaryFolder    = "Build folder: %s"
	summaryOut       = "Output folder: %s"
	summaryPlatform  = "Platform: %s"
	summaryShipped   = "Settings: as shipped"
	summarySettings  = "Settings:"
	summarySetting   = "%s: %s"
	summaryData      = "Saves and settings go in %s on the computer you install it on."
	technicalDetails = "Technical details"
	techSteps        = "The build runs, in the game's folder:"
	techSeed         = "config.toml for a first install:"
	techNoSeed       = "Every option at its default: no config.toml is written."
	techPlan         = "The plan, from package --plan --json:"
)

// Building.
const (
	titleBuild   = "Building"
	buildIntro   = "This takes a while. You can keep using your computer; closing this window stops the build."
	buildPrepare = "Getting ready"
	buildAll     = "%s in all"
	buildPart    = "Part %d of %d"
	buildPct     = "%d%%"
	buildSep     = " · "
	partLog      = "Log"
	partHideLog  = "Hide log"
	partWaiting  = "waiting"
	partNotRun   = "not run"
	partDone     = "done · %s"
	partFailed   = "failed · %s"
	partSkipped  = "not needed"
	partStopped  = "stopped · %s"
	cancelText   = "Cancel"
	stopTitle    = "Are you sure?"
	stopBack     = "Go back"
	stopConfirm  = "Cancel Build"
	quitTitle    = "A build is running"
	// The re-run skips import, setup and generate once they are done and
	// ninja keeps every compiled object, but nothing resumes inside a
	// step and the HUD pack and the staging are redone (the
	// wizard-test-notes design, D6): "most", not "from where it stopped".
	stopText = "Next time, the build reuses most of what already finished."
	quitText = "A build is running. Stop it and quit? Next time, the build reuses most of what already finished."
)

// partNames are the build's parts in the player's words, by the CLI's
// command or stage name; a name not here shows as the CLI prints it.
var partNames = map[string]string{
	"import":      "Copy your game files",
	"enhance hud": "Redraw the HUD and menus",
	"plan":        "Build the game",
	"setup":       "Get the tools",
	"generate":    "Translate the game's code",
	"build":       "Compile",
	"package":     "Make the installer",
}

// Done.
const (
	titleDone        = "Done"
	titleStopped     = "The build stopped"
	doneNext         = "Quit"
	doneReady        = "Your build is ready."
	openBundleFolder = "Open the folder"
	installNowText   = "Install now"
	doneData         = "Saves and settings live in %s on the computer you install it on."
	doneFailed       = "The build did not finish."
	doneStoppedAt    = "It stopped at: %s."
	doneSaid         = "What the build said:"
	doneExit         = "(exit status %d)"
	doneLogs         = "The full logs are in %s."
	doneCanceled     = "You stopped the build."
	openLogFolder    = "Open the log folder"
	doneRetry        = "Retry runs the build again and reuses most of what already finished."
)

// The notice on the Done page (notice.go): the lead and the tail in bold,
// the body names the game.
const (
	noticeLead = "For your own computers only."
	noticeBody = "The bundle holds your own copy of %s and code made from it."
	noticeTail = "Never share, upload or publish it."
)

// installText is docs/packaging.md's install step for each target.
var installText = map[string]string{
	"windows": "Run the setup program on the Windows PC. It installs for your user only and adds a Start menu entry. " +
		"Installing copies the game files again, so the PC needs about as much free space as the bundle takes.",
	"macos":   "Open the .dmg and drag the app to Applications. The first time, right-click the app and choose Open (it is not signed).",
	"steamos": "Copy the .tar to the Steam Deck or Linux PC, unpack it and run ./install.sh --steam in a terminal: it installs the game and adds it to Steam.",
}

// The build folder's line on Welcome and the disk line on Done (D13). A
// player never reads "patch", "base" or a version's commits here: an
// update is for a version, which shows as its date and time.
const (
	folderFoundRelease = "Build folder found: %s %s (%s). This release will reuse it."
	folderFoundDev     = "Build folder found: %s, a developer build at %s. This release will reuse it."
	folderFoundLocal   = "Build folder found: %s at %s. The local source %s will reuse it."
	diskMeasuring      = "Build folder: measuring…"
	diskLine           = "Build folder: %s"
	diskFreeing        = "Build folder: freeing space…"
	diskFreed          = "Freed %s. Build folder: %s"
	diskFreeFailed     = "Build folder: the space could not be freed; the wizard's log says why."
	freeSpaceText      = "Free space"
	freeTitle          = "Free space?"
	freeText           = "This deletes the compiled program and the bundles kept in the build folder. " +
		"Your game files and the translated code stay, and the output folder is not touched. The next build compiles again."
)

// The two paths (the wizard-paths design, D1): the choice on Welcome,
// what the device has now (D2), the changes (D9), and what the Summary,
// the failure page and Done say of an update (D7, D8).
const (
	pathHeading    = "What do you want to make?"
	pathFresh      = "Fresh build"
	pathFreshHint  = "A complete installer: for a first install, a new device, or to start over. Your saves and settings on the device are kept."
	pathUpdate     = "Update build"
	pathUpdateHint = "A small update for a device that already has a build made with this wizard. Only what changed is copied."

	titleHas        = "What does the device have now?"
	hasIntro        = "An update only works on the build it was made for. Pick the one %s has now."
	hasAsking       = "Looking for the builds made here…"
	hasNoPlan       = "The builds made here could not be listed. Pick a file from the device, or make a Fresh build."
	hasNone         = "No build for %s was made in this build folder yet. Pick a file from the device, or make a Fresh build."
	hasInstalled    = "This computer's install: %s"
	hasBuild        = "%s, made %s"
	hasFile         = "A file from the device…"
	hasFileChosen   = "From the device: %s"
	hasFileWhere    = "The file is manifest.json, at %s on %s. Copy it here and pick it."
	hasStatus       = "On the Steam Deck or Linux PC, run ./install.sh status in the game's folder: it shows the update ID."
	hasDetail       = "Update ID %s · %s"
	hasSize         = " · update about %s"
	hasNoTweaks     = "no tweaks"
	hasCannot       = "It cannot take this update (%s)."
	hasUnreadable   = "Its record cannot be read."
	hasSameCode     = "the code is the same: an update to it changes its tweaks"
	pickManifest    = "The device's manifest.json"
	freshInstead    = "Make a Fresh build instead"
	cannotLayout    = "The game data layout changed since that build, so it can't be updated."
	cannotShare     = "This update would be most of the full installer (%s)."
	cannotShareAny  = "This update would be most of the full installer."
	cannotGuard     = "That build has tweaks, and this update does not say whether to keep them. Tick the box to make them new, or clear them."
	cannotGameFiles = "That build was made from other game files, so it can't be updated."
	cannotDeletes   = "This update would remove game files that build has, so it is not made."
	cannotSame      = "Nothing changed since that build."
	cannotKeep      = "The device's tweaks cannot be made again the same here. Tick the box to make them new, or clear them."
	cannotOther     = "An update cannot be made for that build."
	titleChanges    = "Changes"
	changesNow      = "On the device now: %s"
	changesAsking   = "Checking what the update needs…"
	changesClear    = "Clear previous tweaks"
	changesDisc     = "The game files in the build folder are missing or changed: the next page asks for your game."
	changesNoDisc   = "Your game is not needed: the build folder has its files."
	changesConfig   = "The game's settings are in config.toml on the device (%s). An update never changes them."
	changesOverride = "Your config.toml (%s) sets %s = %q, which overrides this build's choice. Remove that line to use it."
	changesStatic   = "If your config.toml on the device sets pack under [textures], it overrides this. The installer will say so."
	summaryAsking   = "Checking what the build will make…"
	summaryRebuild  = "Rebuild everything"
	rebuildHint     = "Slower. Only for when a build misbehaves: your game files and the translated code stay."
	summaryUpdates  = "Updates: %s (update ID %s)"
	summaryFresh    = "Makes: the full installer%s"
	summaryUpdSize  = "Update size: about %s"
	summaryKeeps    = "Keeps: %s"
	summaryChanges  = "Changes: %s"
	summaryNothing  = "Keeps: no tweaks"
	summaryRemoves  = "Removes %d game files from that build."
	summaryRemove1  = "Removes 1 game file from that build."
	doneWrongBuild  = "If the device has another version, the update refuses and changes nothing. Make a Fresh build then."
	failedFresh     = "This update cannot be made for that build. A Fresh build makes the complete installer."
)

// Tweak words by the plan's key: the name on the device, what ticking
// the box does, and what leaving it does when the device has the tweak
// or not, and what Clear does (D9).
type tweakText struct {
	name, made, keeps, none, clear, removes string
}

var tweakTexts = map[string]tweakText{
	"hud_pack": {
		name:    "Sharper HUD",
		made:    "A new sharper HUD is made from your game files and replaces the device's.",
		keeps:   "Unticked: the device's sharper HUD stays as it is.",
		none:    "Unticked: no sharper HUD.",
		clear:   "Removes the sharper HUD from the device's build.",
		removes: "removes the sharper HUD",
	},
}

// manifestWhere is where a target keeps the current build's manifest.json
// (D2): %[1]s is the data folder, %[2]s the app's name.
var manifestWhere = map[string]string{
	"steamos": "%[1]s/current/manifest.json",
	"windows": `%%LOCALAPPDATA%%\Programs\%[2]s\versions\<the version in current.txt>\manifest.json`,
	"macos":   "%[2]s.app/Contents/Resources/manifest.json",
}

// targetDevice is the computer a target installs on, inside a sentence.
var targetDevice = map[string]string{
	"windows": "the Windows PC",
	"macos":   "the Mac",
	"steamos": "the Steam Deck or Linux PC",
}

// applyPatchText is the apply step of an update (docs/bundle.md).
var applyPatchText = map[string]string{
	"windows": "Copy the folder to the Windows PC and double-click apply.exe in it: it updates the installed version.",
	"macos":   "Open the .dmg and double-click apply.command: it updates the app in Applications.",
	"steamos": "Copy the .tar to the Steam Deck or Linux PC, unpack it and run ./install.sh in a terminal: it updates the installed version.",
}
