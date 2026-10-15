package screens

import (
	"sync"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/widget"
)

// maxLines bounds a log pane; the full output is in the CLI's build-logs/.
const maxLines = 5000

// logView is a scrolling list of output lines, safe to append to from any
// goroutine.
type logView struct {
	mu    sync.Mutex
	lines []string
	list  *widget.List
	tee   func(string)
}

func newLogView(tee func(string)) *logView {
	v := &logView{tee: tee}
	v.list = widget.NewList(
		func() int { v.mu.Lock(); defer v.mu.Unlock(); return len(v.lines) },
		func() fyne.CanvasObject {
			l := widget.NewLabel("")
			l.TextStyle = fyne.TextStyle{Monospace: true}
			l.Truncation = fyne.TextTruncateEllipsis
			return l
		},
		func(i widget.ListItemID, o fyne.CanvasObject) {
			v.mu.Lock()
			s := ""
			if i < len(v.lines) {
				s = v.lines[i]
			}
			v.mu.Unlock()
			o.(*widget.Label).SetText(s)
		},
	)
	return v
}

func (v *logView) Append(s string) {
	if v.tee != nil {
		v.tee(s)
	}
	v.mu.Lock()
	v.lines = append(v.lines, s)
	if len(v.lines) > maxLines {
		v.lines = v.lines[len(v.lines)-maxLines:]
	}
	v.mu.Unlock()
	fyne.Do(func() {
		v.list.Refresh()
		v.list.ScrollToBottom()
	})
}

func (v *logView) Clear() {
	v.mu.Lock()
	v.lines = nil
	v.mu.Unlock()
	fyne.Do(v.list.Refresh)
}
