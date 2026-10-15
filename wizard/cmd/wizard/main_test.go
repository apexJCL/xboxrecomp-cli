package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestDry(t *testing.T) {
	var out, errb bytes.Buffer
	t.Setenv("HOME", t.TempDir())
	rc := run([]string{"--dry", "--config", "../../internal/gamecfg/testdata/blinx2.json", "--root", "/r"}, &out, &errb)
	if rc != 0 {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	s := out.String()
	for _, want := range []string{"install folder: /r", "--branch v0.0.0-sample", "run: ./blinx2 package", "--dump DUMP --plain"} {
		if !strings.Contains(s, want) {
			t.Errorf("no %q in:\n%s", want, s)
		}
	}
}

func TestNoConfig(t *testing.T) {
	var out, errb bytes.Buffer
	if rc := run([]string{"--dry"}, &out, &errb); rc != 1 || !strings.Contains(errb.String(), "developer build") {
		t.Fatalf("rc %d: %s", rc, errb.String())
	}
	if rc := run([]string{"--version"}, &out, &errb); rc != 0 || !strings.Contains(out.String(), "dev") {
		t.Fatal(out.String())
	}
	if rc := run([]string{"stray"}, &out, &errb); rc != 2 {
		t.Fatal("a stray argument")
	}
}
