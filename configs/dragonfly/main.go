// SPDX-License-Identifier: Apache-2.0

// Command dfbench is Dragonfly's example server with a console on standard input, built for bedrock-bench.
//
// It starts a Dragonfly server from config.toml the way Dragonfly's own main.go does, and adds what the benchmark
// harness needs and the example server lacks:
//
//   - a console on standard input with the commands the harness sends (console.go);
//   - a version banner naming the Dragonfly build, and a line once a player has entered the world;
//   - a [Bench] table in config.toml for settings Dragonfly only exposes through its Go API (config.go).
//
// With no [Bench] table the server behaves like the example server. Logs go to standard output.
//
// Usage:
//
//	dfbench [-config config.toml]   run the server
//	dfbench -version                print build information as JSON and exit
//	dfbench -defaults               print the default configuration as TOML and exit
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"log/slog"
	"os"
	"runtime"
	"runtime/debug"
	"strings"

	"github.com/df-mc/dragonfly/server/player"
	"github.com/df-mc/dragonfly/server/player/chat"
	"github.com/df-mc/dragonfly/server/world"
	"github.com/go-gl/mathgl/mgl64"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"golang.org/x/mod/module"
)

// Set at build time with -ldflags "-X main.version=... -X main.commit=...".
var (
	version = "dev"
	commit  = ""
)

const (
	dragonflyModule    = "github.com/df-mc/dragonfly"
	gophertunnelModule = "github.com/sandertv/gophertunnel"

	// factsFile receives the runtime facts of a run, next to config.toml.
	factsFile = "dfbench.json"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("dfbench", flag.ContinueOnError)
	fs.SetOutput(stderr)
	configPath := fs.String("config", "config.toml", "configuration `file`; created with the defaults if missing")
	printVersion := fs.Bool("version", false, "print build information as JSON and exit")
	printDefaults := fs.Bool("defaults", false, "print the default configuration as TOML and exit")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "dfbench: unexpected arguments: %v\n", fs.Args())
		return 2
	}

	build := readBuildInfo()
	switch {
	case *printVersion:
		if err := json.NewEncoder(stdout).Encode(build); err != nil {
			fmt.Fprintf(stderr, "dfbench: %v\n", err)
			return 1
		}
		return 0
	case *printDefaults:
		if err := writeDefaults(stdout); err != nil {
			fmt.Fprintf(stderr, "dfbench: %v\n", err)
			return 1
		}
		return 0
	}
	if err := serve(*configPath, build, stdin, stdout); err != nil {
		fmt.Fprintf(stderr, "dfbench: %v\n", err)
		return 1
	}
	return 0
}

// serve runs the server until it is stopped from the console, standard input closes or the process is signalled.
func serve(path string, build buildInfo, stdin io.Reader, stdout io.Writer) error {
	cfg, err := loadConfig(path)
	if err != nil {
		return err
	}
	// The example server logs through slog's default handler, which writes through the log package. Keep that, with
	// microseconds, and send it to stdout so the harness reads one stream.
	log.SetOutput(stdout)
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	slog.SetLogLoggerLevel(cfg.Bench.logLevel())
	chat.Global.Subscribe(chat.StdoutSubscriber{})

	conf, err := cfg.User.Config(slog.Default())
	if err != nil {
		return err
	}
	conf.SaveInterval = cfg.Bench.saveInterval()
	srv := conf.New()
	cfg.Bench.apply(srv.World(), srv.Nether(), srv.End())
	if cfg.Bench.IdleTicking {
		keepTicking(srv.World())
	}
	srv.CloseOnProgramEnd()

	slog.Info(build.banner())
	slog.Info("Bench settings: " + cfg.Bench.String())
	if err := writeFacts(factsFile, build, cfg.Bench, srv.World()); err != nil {
		return err
	}

	players := newRoster()
	srv.Listen()
	slog.Info("Server started.")
	go newConsole(srv, players).run(stdin)

	// The loop body runs inside the transaction that added the player to its world, so the line below is printed
	// once the player is in the world and can be targeted by commands.
	for p := range srv.Accept() {
		players.add(p.Name(), p.H())
		p.Handle(quitHandler{players: players})
		slog.Info("Player spawned: " + p.Name())
	}
	slog.Info("Server stopped.")
	return nil
}

// idleLoaderPos is far from every chunk a benchmark loads, so the idle loader never brings a chunk into tick range.
var idleLoaderPos = mgl64.Vec3{-100000, 0, -100000}

// keepTicking makes w tick while nobody is in it. Dragonfly skips the ticks of a world without viewers, which also
// stops its tick counter. A loader is a viewer; this one has a chunk radius of 0, is never asked to load a chunk and
// sits far away, so it changes nothing but that.
func keepTicking(w *world.World) {
	w.Do(func(tx *world.Tx) {
		loader := world.NewLoader(0, tx.World(), world.NopViewer{})
		loader.Move(tx, idleLoaderPos)
	})
}

// quitHandler removes a player from the console's roster when it leaves the server.
type quitHandler struct {
	player.NopHandler
	players *roster
}

func (h quitHandler) HandleQuit(p *player.Player) {
	h.players.remove(p.Name(), p.H())
}

// buildInfo describes this binary and the Dragonfly build it was linked against.
type buildInfo struct {
	Module          string `json:"module"`
	Version         string `json:"version"`
	VCS             string `json:"vcs,omitempty"`
	Go              string `json:"go"`
	Dragonfly       string `json:"dragonfly"`        // the server tag's version: the release, or the commit's first 7 characters
	DragonflyModule string `json:"dragonfly_module"` // the module version, or the replacement when go.mod replaces it
	Gophertunnel    string `json:"gophertunnel"`
	Protocol        int    `json:"protocol"`
	Minecraft       string `json:"minecraft"`
}

func readBuildInfo() buildInfo {
	b := buildInfo{
		Module:    "dfbench",
		Version:   version,
		VCS:       commit,
		Go:        runtime.Version(),
		Dragonfly: "unknown",
		Protocol:  protocol.CurrentProtocol,
		Minecraft: protocol.CurrentVersion,
	}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return b
	}
	for _, dep := range info.Deps {
		switch dep.Path {
		case dragonflyModule:
			b.DragonflyModule = dep.Version
			b.Dragonfly = serverVersion(dep.Version)
			if dep.Replace != nil {
				// A replaced module is not the pinned build, whatever version it claims.
				b.DragonflyModule = strings.TrimSuffix(dep.Replace.Path+"@"+dep.Replace.Version, "@")
				b.Dragonfly = "devel"
			} else if dep.Version == "(devel)" {
				b.Dragonfly = "devel" // a module from a Go workspace
			}
		case gophertunnelModule:
			b.Gophertunnel = dep.Version
			if dep.Replace != nil {
				b.Gophertunnel = strings.TrimSuffix(dep.Replace.Path+"@"+dep.Replace.Version, "@")
			}
		}
	}
	return b
}

// serverVersion turns a Dragonfly module version into the version of its server tag: v0.11.5 is 0.11.5, and a
// pseudo-version such as v0.11.6-0.20260920163020-1793369709ce, which names a commit, is 1793369.
func serverVersion(v string) string {
	if module.IsPseudoVersion(v) {
		if rev, err := module.PseudoVersionRev(v); err == nil && len(rev) >= 7 {
			return rev[:7]
		}
	}
	return strings.TrimPrefix(v, "v")
}

// banner is the version line the harness checks against the arm it runs.
func (b buildInfo) banner() string {
	return fmt.Sprintf("Dragonfly version %s (module %s, Minecraft %s, protocol %d, %s, GOMAXPROCS %d)",
		b.Dragonfly, b.DragonflyModule, b.Minecraft, b.Protocol, b.Go, runtime.GOMAXPROCS(0))
}

// facts are written to factsFile when the server starts, for the harness to record.
type facts struct {
	buildInfo
	GOMAXPROCS      int    `json:"gomaxprocs"`
	NumCPU          int    `json:"num_cpu"`
	Difficulty      string `json:"difficulty"`
	DefaultGameMode string `json:"default_game_mode"`
	TickRange       *int   `json:"tick_range"`
	SaveIntervalS   int    `json:"save_interval_s"`
	IdleTicking     bool   `json:"idle_ticking"`
	LogLevel        string `json:"log_level"`
}

// writeFacts records the build, the Go runtime's view of the CPUs and the world settings in effect. Difficulty and
// game mode are read back from the world.
func writeFacts(path string, build buildInfo, bench benchConfig, w *world.World) error {
	f := facts{
		buildInfo:       build,
		GOMAXPROCS:      runtime.GOMAXPROCS(0),
		NumCPU:          runtime.NumCPU(),
		Difficulty:      difficultyName(w.Difficulty()),
		DefaultGameMode: gameModeName(w.DefaultGameMode()),
		TickRange:       bench.TickRange,
		SaveIntervalS:   int(bench.saveInterval().Seconds()),
		IdleTicking:     bench.IdleTicking,
		LogLevel:        bench.LogLevel,
	}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}
