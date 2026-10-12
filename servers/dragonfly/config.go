// SPDX-License-Identifier: Apache-2.0

package main

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/df-mc/dragonfly/server"
	"github.com/df-mc/dragonfly/server/world"
	"github.com/df-mc/dragonfly/server/world/mcdb/leveldat"
	"github.com/pelletier/go-toml"
)

// config is the file dfbench reads: Dragonfly's own configuration plus the [Bench] table.
type config struct {
	User  server.UserConfig
	Bench benchConfig
}

// benchConfig holds settings that Dragonfly's config.toml does not cover, so its example server always runs them at
// their defaults. Each default below is what the example server does. The world settings (TickRange, Difficulty,
// GameMode) apply only when present; otherwise the world keeps what its level.dat says, and -defaults shows the
// values of a new world.
type benchConfig struct {
	// LogLevel is the lowest level logged: debug, info, warn or error. The example server logs at debug.
	LogLevel string
	// IdleTicking keeps the overworld ticking while no player is in it (see keepTicking).
	IdleTicking bool
	// SaveIntervalSeconds is how often the worlds are saved to disk. 0 keeps Dragonfly's default of 10 minutes; a
	// negative value turns autosave off.
	SaveIntervalSeconds int
	// TickRange is the radius in chunks around each player whose blocks and entities tick.
	TickRange *int `toml:",omitempty"`
	// Difficulty is peaceful, easy, normal or hard.
	Difficulty *string `toml:",omitempty"`
	// GameMode is the game mode of players who join for the first time: survival, creative, adventure or spectator.
	GameMode *string `toml:",omitempty"`
}

// dragonflySaveInterval is server.Config's documented default autosave interval.
const dragonflySaveInterval = 10 * time.Minute

var (
	logLevels = map[string]slog.Level{
		"debug": slog.LevelDebug, "info": slog.LevelInfo, "warn": slog.LevelWarn, "error": slog.LevelError,
	}
	difficulties = []string{"peaceful", "easy", "normal", "hard"}             // by Dragonfly difficulty id
	gameModes    = []string{"survival", "creative", "adventure", "spectator"} // by Dragonfly game mode id
)

// exampleBench is the [Bench] table that reproduces the example server. It leaves the world settings unset.
func exampleBench() benchConfig {
	return benchConfig{LogLevel: "debug", SaveIntervalSeconds: int(dragonflySaveInterval.Seconds())}
}

// defaultBench is exampleBench with the world settings filled in from a new world in Dragonfly's LevelDB provider (the
// one World.SaveData = true selects), for -defaults.
func defaultBench() benchConfig {
	var data leveldat.Data
	data.FillDefault()
	settings := data.Settings()
	tickRange := int(settings.TickRange)
	difficulty := difficultyName(settings.Difficulty)
	gameMode := gameModeName(settings.DefaultGameMode)
	b := exampleBench()
	b.TickRange, b.Difficulty, b.GameMode = &tickRange, &difficulty, &gameMode
	return b
}

// loadConfig reads path on top of the defaults, like the example server reads its config.toml: keys that are not in
// the file keep their default. A missing file is created with Dragonfly's defaults, as the example server does.
func loadConfig(path string) (config, error) {
	cfg := config{User: server.DefaultConfig(), Bench: exampleBench()}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		encoded, err := toml.Marshal(cfg.User)
		if err != nil {
			return cfg, fmt.Errorf("encode default config: %w", err)
		}
		if err := os.WriteFile(path, encoded, 0o644); err != nil {
			return cfg, fmt.Errorf("create default config: %w", err)
		}
		return cfg, nil
	}
	if err != nil {
		return cfg, fmt.Errorf("read config: %w", err)
	}
	if err := parseConfig(data, &cfg); err != nil {
		return cfg, fmt.Errorf("%s: %w", path, err)
	}
	return cfg, nil
}

// parseConfig decodes data onto cfg and checks the [Bench] values.
func parseConfig(data []byte, cfg *config) error {
	tree, err := toml.LoadBytes(data)
	if err != nil {
		return fmt.Errorf("decode: %w", err)
	}
	if err := checkKeys(tree, "", "Network", "Server", "World", "Players", "Resources", "Bench"); err != nil {
		return err
	}
	if err := tree.Unmarshal(&cfg.User); err != nil {
		return fmt.Errorf("decode: %w", err)
	}
	if bench, ok := tree.Get("Bench").(*toml.Tree); ok {
		if err := checkKeys(bench, "Bench.", "LogLevel", "IdleTicking", "SaveIntervalSeconds", "TickRange", "Difficulty", "GameMode"); err != nil {
			return err
		}
		if err := bench.Unmarshal(&cfg.Bench); err != nil {
			return fmt.Errorf("decode [Bench]: %w", err)
		}
	}
	return cfg.Bench.validate()
}

// checkKeys rejects keys of t that are not known, so a misspelt table or setting fails instead of passing for a
// default. Keys inside Dragonfly's own tables are left to Dragonfly, which ignores unknown ones.
func checkKeys(t *toml.Tree, prefix string, known ...string) error {
	for _, key := range t.Keys() {
		if !slices.Contains(known, key) {
			return fmt.Errorf("unknown key %s%s (known: %s)", prefix, key, strings.Join(known, ", "))
		}
	}
	return nil
}

func (b benchConfig) validate() error {
	if _, ok := logLevels[b.LogLevel]; !ok {
		return fmt.Errorf("Bench.LogLevel must be debug, info, warn or error, not %q", b.LogLevel)
	}
	if b.TickRange != nil && *b.TickRange < 0 {
		return fmt.Errorf("Bench.TickRange must not be negative, got %d", *b.TickRange)
	}
	if b.Difficulty != nil {
		if _, ok := parseDifficulty(*b.Difficulty); !ok {
			return fmt.Errorf("Bench.Difficulty must be one of %s, not %q", strings.Join(difficulties, ", "), *b.Difficulty)
		}
	}
	if b.GameMode != nil {
		if _, ok := parseGameMode(*b.GameMode); !ok {
			return fmt.Errorf("Bench.GameMode must be one of %s, not %q", strings.Join(gameModes, ", "), *b.GameMode)
		}
	}
	return nil
}

func (b benchConfig) logLevel() slog.Level { return logLevels[b.LogLevel] }

// saveInterval is the value for server.Config.SaveInterval.
func (b benchConfig) saveInterval() time.Duration {
	switch {
	case b.SaveIntervalSeconds == 0:
		return dragonflySaveInterval
	case b.SaveIntervalSeconds < 0:
		return -1
	}
	return time.Duration(b.SaveIntervalSeconds) * time.Second
}

// apply sets the world settings that are present. The three worlds share one set of settings; each is set anyway.
func (b benchConfig) apply(worlds ...*world.World) {
	for _, w := range worlds {
		if b.TickRange != nil {
			w.SetTickRange(*b.TickRange)
		}
		if b.Difficulty != nil {
			d, _ := parseDifficulty(*b.Difficulty)
			w.SetDifficulty(d)
		}
		if b.GameMode != nil {
			m, _ := parseGameMode(*b.GameMode)
			w.SetDefaultGameMode(m)
		}
	}
}

// String summarises the settings for the log.
func (b benchConfig) String() string {
	kept := func(s *string) string {
		if s == nil {
			return "from level.dat"
		}
		return *s
	}
	tickRange := "from level.dat"
	if b.TickRange != nil {
		tickRange = fmt.Sprint(*b.TickRange)
	}
	autosave := "off"
	if d := b.saveInterval(); d > 0 {
		autosave = "every " + d.String()
	}
	return fmt.Sprintf("tick range %s, difficulty %s, game mode %s, autosave %s, idle ticking %t, log level %s",
		tickRange, kept(b.Difficulty), kept(b.GameMode), autosave, b.IdleTicking, b.LogLevel)
}

// writeDefaults prints Dragonfly's default configuration followed by the default [Bench] table. The harness compares
// what it writes against this to record each change from the defaults.
func writeDefaults(w io.Writer) error {
	user, err := toml.Marshal(server.DefaultConfig())
	if err != nil {
		return err
	}
	bench, err := toml.Marshal(struct{ Bench benchConfig }{defaultBench()})
	if err != nil {
		return err
	}
	_, err = fmt.Fprintf(w, "# Defaults of dfbench %s on Dragonfly %s: a missing key in config.toml means this value.\n%s\n%s",
		version, readBuildInfo().Dragonfly, user, bench)
	return err
}

func parseDifficulty(name string) (world.Difficulty, bool) {
	for id, n := range difficulties {
		if n == name {
			return world.DifficultyByID(id)
		}
	}
	return nil, false
}

func difficultyName(d world.Difficulty) string {
	if id, ok := world.DifficultyID(d); ok && id < len(difficulties) {
		return difficulties[id]
	}
	return "unknown"
}

func parseGameMode(name string) (world.GameMode, bool) {
	for id, n := range gameModes {
		if n == name {
			return world.GameModeByID(id)
		}
	}
	return nil, false
}

func gameModeName(m world.GameMode) string {
	if id, ok := world.GameModeID(m); ok && id < len(gameModes) {
		return gameModes[id]
	}
	return "unknown"
}
