// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
)

func TestVersionPrintsBuildInfo(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"-version"}, strings.NewReader(""), &out, &errOut); code != core.ExitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var b core.BuildInfo
	if err := json.Unmarshal(out.Bytes(), &b); err != nil {
		t.Fatalf("not JSON: %q", out.String())
	}
	if b.Module != "loadbot" || b.Protocol != 2193 || b.Patches == nil || !strings.HasPrefix(b.Go, "go") {
		t.Errorf("build info = %+v", b)
	}
}

func TestSelftestOutputEmitsEveryEvent(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"-selftest-output", "-n", "4"}, strings.NewReader(""), &out, &errOut); code != core.ExitOK {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	seen := map[string]bool{}
	sc := bufio.NewScanner(&out)
	for sc.Scan() {
		var ev map[string]any
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			t.Fatalf("bad line %q", sc.Text())
		}
		seen[ev["ev"].(string)] = true
		if ev["ev"] == core.EvHello && ev["flags"].(map[string]any)["n"] != 4.0 {
			t.Errorf("hello flags = %v", ev["flags"])
		}
	}
	if len(seen) != 21 {
		t.Errorf("selftest emitted %d event kinds, want 21: %v", len(seen), seen)
	}
}

func TestUsageErrorsExit1(t *testing.T) {
	for _, args := range [][]string{
		{},
		{"-addr", "127.0.0.1:1", "-n", "0"},
		{"-addr", "127.0.0.1:1", "extra"},
		{"-no-such-flag"},
	} {
		var out, errOut bytes.Buffer
		if code := run(args, strings.NewReader(""), &out, &errOut); code != core.ExitUsage {
			t.Errorf("%v: exit %d, want %d", args, code, core.ExitUsage)
		}
		if out.Len() != 0 {
			t.Errorf("%v: wrote to stdout: %q", args, out.String())
		}
	}
}
