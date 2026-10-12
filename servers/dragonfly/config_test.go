// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/df-mc/dragonfly/server"
)

func newConfig() config {
	return config{User: server.DefaultConfig(), Bench: exampleBench()}
}

func TestPartialConfigKeepsDefaults(t *testing.T) {
	cfg := newConfig()
	data := "[Players]\n  MaxCount = 200\n\n[Bench]\n  TickRange = 4\n  LogLevel = \"info\"\n"
	if err := parseConfig([]byte(data), &cfg); err != nil {
		t.Fatal(err)
	}
	want := server.DefaultConfig()
	want.Players.MaxCount = 200
	if !reflect.DeepEqual(cfg.User, want) {
		t.Errorf("user config = %+v, want the defaults with MaxCount 200", cfg.User)
	}
	if cfg.Bench.TickRange == nil || *cfg.Bench.TickRange != 4 {
		t.Errorf("TickRange = %v, want 4", cfg.Bench.TickRange)
	}
	if cfg.Bench.Difficulty != nil || cfg.Bench.GameMode != nil {
		t.Errorf("absent world settings must stay unset, got difficulty %v, game mode %v", cfg.Bench.Difficulty, cfg.Bench.GameMode)
	}
	if cfg.Bench.LogLevel != "info" || cfg.Bench.SaveIntervalSeconds != 600 || cfg.Bench.IdleTicking {
		t.Errorf("bench = %+v", cfg.Bench)
	}
}

// TestHarnessConfig reads testdata/config.toml, which the harness's adapter test checks is exactly what it writes for
// configs/spread-100.toml.
func TestHarnessConfig(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "config.toml"))
	if err != nil {
		t.Fatal(err)
	}
	cfg := newConfig()
	if err := parseConfig(data, &cfg); err != nil {
		t.Fatal(err)
	}
	u := cfg.User
	if u.Network.Address != "127.0.0.1:19672" || u.Server.AuthEnabled || u.Server.Name != "bedrock-bench" ||
		u.Players.MaxCount != 200 || u.Players.MaximumChunkRadius != 10 || u.World.Folder != "bench" {
		t.Errorf("user config = %+v", u)
	}
	if !reflect.DeepEqual(u.Network.Transport, []string{"raknet"}) || !u.World.SaveData || !u.Players.SaveData {
		t.Errorf("unlisted or default-valued keys changed: %+v", u)
	}
	b := cfg.Bench
	if b.LogLevel != "info" || !b.IdleTicking || b.saveInterval() != 5*time.Minute || *b.TickRange != 4 ||
		*b.Difficulty != "easy" || *b.GameMode != "survival" {
		t.Errorf("bench = %+v", b)
	}
}

func TestBenchRejectsBadValues(t *testing.T) {
	for _, data := range []string{
		"[Bench]\nTickRang = 4",
		"[Bench]\nLogLevel = \"verbose\"",
		"[Bench]\nTickRange = -1",
		"[Bench]\nDifficulty = \"nightmare\"",
		"[Bench]\nGameMode = \"flying\"",
		"[Bench]\nIdleTicking = \"yes\"",
		"[Players\nMaxCount = 1",
		"[Player]\nMaxCount = 1",
	} {
		cfg := newConfig()
		if err := parseConfig([]byte(data), &cfg); err == nil {
			t.Errorf("%q: accepted", data)
		}
	}
}

func TestDefaultsReadBackAsDefaults(t *testing.T) {
	var out bytes.Buffer
	if err := writeDefaults(&out); err != nil {
		t.Fatal(err)
	}
	cfg := config{}
	if err := parseConfig(out.Bytes(), &cfg); err != nil {
		t.Fatalf("defaults do not parse: %v\n%s", err, out.String())
	}
	if !reflect.DeepEqual(cfg.User, server.DefaultConfig()) {
		t.Errorf("user config = %+v, want server.DefaultConfig()", cfg.User)
	}
	if !reflect.DeepEqual(cfg.Bench, defaultBench()) {
		t.Errorf("bench = %+v, want %+v", cfg.Bench, defaultBench())
	}
}

func TestDefaultBenchMatchesTheExampleServer(t *testing.T) {
	b := defaultBench()
	if b.LogLevel != "debug" || b.IdleTicking || b.saveInterval() != 10*time.Minute {
		t.Errorf("defaults differ from Dragonfly's main.go and server.Config: %+v", b)
	}
	if b.TickRange == nil || b.Difficulty == nil || b.GameMode == nil {
		t.Fatalf("defaults must show the world settings of a new world: %+v", b)
	}
}

func TestSaveInterval(t *testing.T) {
	for seconds, want := range map[int]time.Duration{0: 10 * time.Minute, 300: 5 * time.Minute, -1: -1} {
		if got := (benchConfig{SaveIntervalSeconds: seconds}).saveInterval(); got != want {
			t.Errorf("SaveIntervalSeconds %d: %v, want %v", seconds, got, want)
		}
	}
}

func TestMissingConfigIsCreatedWithTheDefaults(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.toml")
	cfg, err := loadConfig(path)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(cfg.User, server.DefaultConfig()) {
		t.Errorf("user config = %+v", cfg.User)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "MaximumChunkRadius = 32") {
		t.Errorf("created file lacks the defaults:\n%s", data)
	}
	again, err := loadConfig(path)
	if err != nil || !reflect.DeepEqual(again, cfg) {
		t.Errorf("reloading the created file: %+v, %v", again, err)
	}
}

func TestNamesRoundTrip(t *testing.T) {
	for _, name := range difficulties {
		d, ok := parseDifficulty(name)
		if !ok || difficultyName(d) != name {
			t.Errorf("difficulty %q does not round-trip", name)
		}
	}
	for _, name := range gameModes {
		m, ok := parseGameMode(name)
		if !ok || gameModeName(m) != name {
			t.Errorf("game mode %q does not round-trip", name)
		}
	}
}
