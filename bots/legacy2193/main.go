// SPDX-License-Identifier: Apache-2.0

// Command legacy2193 joins offline-mode bots to a Minecraft Bedrock server that speaks protocol 2193, walks them in
// circles and reports what they receive as JSON lines on stdout. In the benchmark that server is LeviLamina, which
// has no release for a newer Bedrock version.
//
// It is bots/loadbot built against upstream gophertunnel v1.62.0, the last release for protocol 2193. Flags, events,
// the stdin protocol and all behaviour come from bots/core and are the same as loadbot's; see bots/README.md. A
// behaviour change to loadbot must be made here too. The non-test files mirror loadbot's one for one, and apart from
// names they differ only where the gophertunnel API does.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"syscall"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
)

// Set at build time with -ldflags "-X main.version=... -X main.commit=...".
var (
	version = "dev"
	commit  = ""
)

const gophertunnelModule = "github.com/sandertv/gophertunnel"

// carriedPatches lists the gophertunnel patches this build carries (bots/README.md#patches). Keep it in sync with
// the replace directive in go.mod.
var carriedPatches = []string{}

func main() {
	os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("legacy2193", flag.ContinueOnError)
	fs.SetOutput(stderr)
	fs.Usage = func() {
		fmt.Fprintln(stderr, "usage: legacy2193 -addr HOST:PORT [flags]")
		fs.PrintDefaults()
	}
	var cfg core.Config
	cfg.Register(fs)
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return core.ExitOK
		}
		return core.ExitUsage
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(stderr, "legacy2193: unexpected arguments: %v\n", fs.Args())
		return core.ExitUsage
	}

	build := buildInfo()
	switch {
	case cfg.Version:
		enc := json.NewEncoder(stdout)
		if err := enc.Encode(build); err != nil {
			return core.ExitUsage
		}
		return core.ExitOK
	case cfg.SelftestOutput:
		core.Selftest(core.NewEmitter(stdout, core.NewSystemClock()), build, core.FlagValues(fs), cfg.Intervals())
		return core.ExitOK
	}
	if err := cfg.Validate(); err != nil {
		fmt.Fprintf(stderr, "legacy2193: %v\n", err)
		return core.ExitUsage
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	fleet := core.NewFleet(&cfg, frontend{}, core.NewEmitter(stdout, core.NewSystemClock()), stderr)
	return fleet.Run(ctx, stdin, core.FlagValues(fs))
}

// buildInfo describes this binary: the stamped version and commit, the Go toolchain, and the gophertunnel module
// it was linked against (the replacement, if go.mod replaces it).
func buildInfo() core.BuildInfo {
	b := core.BuildInfo{
		Module:   "legacy2193",
		Version:  version,
		VCS:      commit,
		Go:       runtime.Version(),
		Patches:  append([]string{}, carriedPatches...),
		Protocol: protocol.CurrentProtocol,
	}
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, dep := range info.Deps {
			if dep.Path != gophertunnelModule {
				continue
			}
			b.Gophertunnel = dep.Version
			if dep.Replace != nil {
				b.Gophertunnel = dep.Replace.Path + "@" + dep.Replace.Version
			}
		}
	}
	return b
}
