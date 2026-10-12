// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/df-mc/dragonfly/server/world"
)

func TestVersionFlag(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"-version"}, nil, &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	var b buildInfo
	if err := json.Unmarshal(out.Bytes(), &b); err != nil {
		t.Fatal(err)
	}
	if b.Module != "dfbench" || b.Protocol <= 0 || b.Minecraft == "" || b.Go == "" {
		t.Errorf("build info = %+v", b)
	}
}

func TestDefaultsFlag(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"-defaults"}, nil, &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s", code, errOut.String())
	}
	for _, want := range []string{"[Network]", "[Players]", "[Bench]", "IdleTicking = false"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("defaults lack %q:\n%s", want, out.String())
		}
	}
}

func TestBadArguments(t *testing.T) {
	for _, args := range [][]string{{"extra"}, {"-no-such-flag"}} {
		if code := run(args, nil, io.Discard, io.Discard); code != 2 {
			t.Errorf("%q: exit %d, want 2", args, code)
		}
	}
}

// newTestWorld returns a world with nothing in it that ticks on its own, like a server's overworld.
func newTestWorld(t *testing.T) *world.World {
	t.Helper()
	w := world.Config{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Provider: world.NopProvider{}}.New()
	t.Cleanup(func() { _ = w.Close() })
	return w
}

func currentTick(t *testing.T, w *world.World) int64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	tick, err := world.Call(ctx, w, func(tx *world.Tx) (int64, error) { return tx.CurrentTick(), nil })
	if err != nil {
		t.Fatal(err)
	}
	return tick
}

// TestIdleTicking checks the reason IdleTicking exists against the pinned Dragonfly: a world nobody is in stops its
// tick counter, and the idle loader keeps it running.
func TestIdleTicking(t *testing.T) {
	if testing.Short() {
		t.Skip("waits for real ticks")
	}
	idle := newTestWorld(t)
	kept := newTestWorld(t)
	keepTicking(kept)
	time.Sleep(600 * time.Millisecond)
	if tick := currentTick(t, idle); tick > 1 {
		t.Errorf("a world without viewers ticked %d times; IdleTicking may no longer be needed", tick)
	}
	if tick := currentTick(t, kept); tick < 3 {
		t.Errorf("the idle loader kept the world at %d ticks in 600 ms", tick)
	}
}

func TestWriteFacts(t *testing.T) {
	w := newTestWorld(t)
	easy, survival, tickRange := "easy", "survival", 4
	bench := benchConfig{LogLevel: "info", IdleTicking: true, SaveIntervalSeconds: 300,
		TickRange: &tickRange, Difficulty: &easy, GameMode: &survival}
	bench.apply(w)
	path := filepath.Join(t.TempDir(), factsFile)
	if err := writeFacts(path, readBuildInfo(), bench, w); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"module": "dfbench", "difficulty": "easy", "default_game_mode": "survival",
		"tick_range": 4.0, "save_interval_s": 300.0, "idle_ticking": true, "log_level": "info"}
	for key, value := range want {
		if got[key] != value {
			t.Errorf("%s = %v, want %v", key, got[key], value)
		}
	}
	if n, ok := got["gomaxprocs"].(float64); !ok || n < 1 {
		t.Errorf("gomaxprocs = %v", got["gomaxprocs"])
	}
}
