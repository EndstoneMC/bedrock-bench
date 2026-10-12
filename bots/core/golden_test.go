// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bufio"
	"bytes"
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"
)

var update = flag.Bool("update", false, "rewrite testdata/golden.jsonl")

// TestSelftestGolden pins the exact event output. tests/unit/test_bots.py validates the same file against
// schemas/bot-event.schema.json, which ties the Go types to the schema.
func TestSelftestGolden(t *testing.T) {
	var buf bytes.Buffer
	build := BuildInfo{
		Module: "loadbot", Version: "0.1.0", VCS: "0123456789ab", Go: "go1.26.3",
		Gophertunnel: "v1.62.0", Protocol: 2193,
	}
	flags := map[string]any{"addr": "127.0.0.1:19672", "n": 100, "prefix": "a", "csg": true, "radius": 16}
	Selftest(NewEmitter(&buf, &fakeClock{}), build, flags, Intervals{Progress: 5000, Stat: 10000, Pkt: 60000})

	path := filepath.Join("testdata", "golden.jsonl")
	if *update {
		if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("%v (run go test -run TestSelftestGolden -update to create it)", err)
	}
	if !bytes.Equal(buf.Bytes(), want) {
		t.Errorf("selftest output differs from %s; rerun with -update if the change is intended\ngot:\n%s", path, buf.String())
	}
}

func TestSelftestCoversEveryEvent(t *testing.T) {
	var buf bytes.Buffer
	Selftest(NewEmitter(&buf, &fakeClock{}), BuildInfo{Module: "loadbot", Version: "x", Protocol: 1}, map[string]any{}, Intervals{1, 1, 1})
	seen := map[string]bool{}
	sc := bufio.NewScanner(&buf)
	for sc.Scan() {
		var ev struct {
			V  int    `json:"v"`
			Ev string `json:"ev"`
		}
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			t.Fatalf("bad line %s: %v", sc.Text(), err)
		}
		if ev.V != EventVersion {
			t.Errorf("line %s: v=%d", sc.Text(), ev.V)
		}
		seen[ev.Ev] = true
	}
	all := []string{
		EvHello, EvDial, EvSpawned, EvJoinFailed, EvTP, EvFirstChunk, EvProgress, EvStat, EvPkt, EvRTT, EvDied,
		EvRespawned, EvDim, EvLeft, EvDropped, EvRejoin, EvKicked, EvDecodeError, EvAllJoined, EvBotSummary, EvDone,
	}
	for _, ev := range all {
		if !seen[ev] {
			t.Errorf("selftest does not emit %q", ev)
		}
	}
	if len(seen) != len(all) {
		t.Errorf("selftest emits %d kinds, want %d", len(seen), len(all))
	}
}
