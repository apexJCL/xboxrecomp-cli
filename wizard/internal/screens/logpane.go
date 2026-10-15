package screens

import (
	"strings"
	"sync"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

const (
	// maxLines bounds a log pane; the full output is in the CLI's build-logs/.
	maxLines = 5000
	// shownLines is what the expanded pane shows. A selectable label is
	// laid out whole on every refresh, so it holds the tail; Copy takes
	// every kept line.
	shownLines = 500
	// paneDelay coalesces a fast stream of lines into one refresh.
	paneDelay = 100 * time.Millisecond
	// paneHeight is the expanded text box's height: the page keeps its
	// table or bar above it.
	paneHeight = 180
)

// logPane is the output of the checks or the build, safe to append to
// from any goroutine. Collapsed (the default) it is one row: a Details
// toggle and the newest line. Expanded it is a read-only, selectable
// monospace text box with a Copy button: a player asked for the log can
// select or copy it.
type logPane struct {
	mu    sync.Mutex
	lines []string
	tee   func(string)
	armed bool
	open  bool
	tone  fyne.ThemeColorName // the newest line's colour; "" is quiet

	obj       *fyne.Container // what the page lays out
	collapsed *fyne.Container
	expanded  *fyne.Container
	last      *quietText
	text      *widget.Label
	scroll    *container.Scroll
	show      *widget.Button
	hide      *widget.Button
	copyBtn   *widget.Button
	relayout  func() // the page's refresh when the pane's height changes

	// after runs f on the UI goroutine after d. The tests replace it to
	// flush by hand: Fyne's test driver runs fyne.Do inline on the timer's
	// goroutine, which would race the test's own flush.
	after func(d time.Duration, f func())
}

func uiAfter(d time.Duration, f func()) { time.AfterFunc(d, func() { fyne.Do(f) }) }

func newLogPane(tee func(string)) *logPane {
	p := &logPane{tee: tee, after: uiAfter}
	p.last = newQuiet("", fyne.TextAlignLeading, fyne.TextStyle{Monospace: true})
	p.last.Truncation = fyne.TextTruncateEllipsis
	p.text = widget.NewLabel("")
	p.text.TextStyle = fyne.TextStyle{Monospace: true}
	p.text.Selectable = true
	p.scroll = container.NewScroll(p.text)
	p.scroll.SetMinSize(fyne.NewSize(0, paneHeight))

	p.show = widget.NewButtonWithIcon(detailsText, theme.MenuDropUpIcon(), func() { p.setOpen(true) })
	p.show.Importance = widget.LowImportance
	p.hide = widget.NewButtonWithIcon(hideDetailsText, theme.MenuDropDownIcon(), func() { p.setOpen(false) })
	p.hide.Importance = widget.LowImportance
	p.copyBtn = widget.NewButtonWithIcon(copyText, theme.ContentCopyIcon(), p.copyAll)
	p.copyBtn.Importance = widget.LowImportance

	p.collapsed = container.NewBorder(nil, nil, p.show, nil, p.last)
	p.expanded = container.NewBorder(container.NewHBox(p.hide, layout.NewSpacer(), p.copyBtn), nil, nil, nil, p.scroll)
	p.expanded.Hide()
	p.obj = container.NewStack(p.collapsed, p.expanded)
	return p
}

func (p *logPane) setOpen(open bool) {
	p.open = open
	if open {
		p.collapsed.Hide()
		p.expanded.Show()
		p.scroll.ScrollToBottom()
	} else {
		p.expanded.Hide()
		p.collapsed.Show()
	}
	p.obj.Refresh()
	if p.relayout != nil {
		p.relayout()
	}
}

func (p *logPane) Append(s string) {
	if p.tee != nil {
		p.tee(s)
	}
	p.mu.Lock()
	p.tone = ""
	p.lines = append(p.lines, s)
	if len(p.lines) > maxLines {
		p.lines = p.lines[len(p.lines)-maxLines:]
	}
	p.arm()
	p.mu.Unlock()
}

// arm schedules one flush; p.mu is held.
func (p *logPane) arm() {
	if p.armed {
		return
	}
	p.armed = true
	p.after(paneDelay, p.flush)
}

// Fail appends a line that reports a failure: the collapsed pane shows it
// in the error colour until the next line.
func (p *logPane) Fail(s string) {
	p.Append(s)
	p.mu.Lock()
	p.tone = theme.ColorNameError
	p.mu.Unlock()
}

func (p *logPane) Clear() {
	p.mu.Lock()
	p.tone = ""
	p.lines = nil
	p.arm()
	p.mu.Unlock()
}

// flush puts the kept lines into the labels, on the UI goroutine.
func (p *logPane) flush() {
	p.mu.Lock()
	p.armed = false
	last, shown := "", p.lines
	if len(shown) > 0 {
		last = shown[len(shown)-1]
	}
	if len(shown) > shownLines {
		shown = shown[len(shown)-shownLines:]
	}
	text := strings.Join(shown, "\n")
	tone := p.tone
	p.mu.Unlock()
	p.last.SetColor(tone)
	p.last.SetText(strings.TrimSpace(last))
	p.text.SetText(text)
	if p.open {
		p.scroll.ScrollToBottom()
	}
}

// Tail is the last n kept lines (fewer when the pane holds fewer).
func (p *logPane) Tail(n int) []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.lines[max(len(p.lines)-n, 0):]...)
}

// all is every kept line, for Copy.
func (p *logPane) all() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	return strings.Join(p.lines, "\n")
}

func (p *logPane) copyAll() {
	fyne.CurrentApp().Clipboard().SetContent(p.all())
}
