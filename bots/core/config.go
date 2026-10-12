// SPDX-License-Identifier: Apache-2.0

// Package core is the protocol-independent half of the load bot: flags, the JSONL event stream, chunk accounting,
// the circle walk, identities, sub-chunk request ranges, the stdin control protocol and the fleet that runs bots
// staggered and reports on them. A protocol frontend (bots/loadbot, bots/legacy1001) implements Frontend and
// translates packets into calls on Session. Nothing in this package imports gophertunnel.
package core

import (
	"errors"
	"flag"
	"fmt"
	"sort"
	"strings"
	"time"
)

// MaxRadius is the largest chunk radius a bot may request; the wire field is a byte.
const MaxRadius = 96

// Walk modes.
const (
	WalkCircle = "circle"
	WalkNone   = "none"
)

// Decode error policies.
const (
	DecodeCount = "count"
	DecodeFail  = "fail"
)

// Config holds every flag shared by the protocol frontends. Field defaults are set by Register.
type Config struct {
	Addr             string
	N                int
	Prefix           string
	Stagger          time.Duration
	CSG              bool
	Radius           int
	LoadScreen       bool
	Walk             string
	SpawnTimeout     time.Duration
	JoinRetries      int
	FakeXUID         bool
	IdentitySeed     string
	Control          bool
	ProgressInterval time.Duration
	StatInterval     time.Duration
	PktInterval      time.Duration
	ProbeBots        int
	DecodeErrors     string
	SelftestOutput   bool
	Version          bool
}

// Register adds the shared flags to fs.
func (c *Config) Register(fs *flag.FlagSet) {
	fs.StringVar(&c.Addr, "addr", "", "server address HOST:PORT (required)")
	fs.IntVar(&c.N, "n", 1, "number of bots")
	fs.StringVar(&c.Prefix, "prefix", "b", "bot name prefix; names are prefix + %02d index")
	fs.DurationVar(&c.Stagger, "stagger", 300*time.Millisecond, "spacing between join starts")
	fs.BoolVar(&c.CSG, "csg", false, "report client-side chunk generation support and memory tier 4")
	fs.IntVar(&c.Radius, "radius", 16, "chunk radius requested after spawn when not 16; servers clamp it")
	fs.BoolVar(&c.LoadScreen, "loadscreen", true, "send loading screen start and end after spawn and after a dimension change")
	fs.StringVar(&c.Walk, "walk", WalkCircle, "movement: circle or none")
	fs.DurationVar(&c.SpawnTimeout, "spawn-timeout", 60*time.Second, "deadline for dial, login and spawn of one join attempt")
	fs.IntVar(&c.JoinRetries, "join-retries", 0, "extra attempts for a failed first join")
	fs.BoolVar(&c.FakeXUID, "fake-xuid", false, "send XUID 2535400000000000+index in the offline login")
	fs.StringVar(&c.IdentitySeed, "identity-seed", "bedrock-bench", "UUIDv5 namespace for bot identities (a UUID, or any string)")
	fs.BoolVar(&c.Control, "control", false, "accept JSON control commands on stdin besides \"quit\"")
	fs.DurationVar(&c.ProgressInterval, "progress-interval", 5*time.Second, "interval of per-bot progress events")
	fs.DurationVar(&c.StatInterval, "stat-interval", 10*time.Second, "interval of aggregate stat events")
	fs.DurationVar(&c.PktInterval, "pkt-interval", 60*time.Second, "interval of packet-mix events")
	fs.IntVar(&c.ProbeBots, "probe-bots", 0, "bots (lowest indices) that measure round trips with latency probes")
	fs.StringVar(&c.DecodeErrors, "decode-errors", DecodeCount, "count: report decode errors; fail: stop and exit 3 on the first")
	fs.BoolVar(&c.SelftestOutput, "selftest-output", false, "print one of every event to stdout and exit")
	fs.BoolVar(&c.Version, "version", false, "print build information as JSON and exit")
}

// Validate checks the flag values. It is not called for -version and -selftest-output.
func (c *Config) Validate() error {
	var errs []error
	if c.Addr == "" {
		errs = append(errs, errors.New("-addr is required"))
	}
	if c.N < 1 {
		errs = append(errs, fmt.Errorf("-n must be at least 1, got %d", c.N))
	}
	if c.Prefix == "" || strings.ContainsAny(c.Prefix, " \t\r\n\"") {
		errs = append(errs, fmt.Errorf("-prefix must be a non-empty name without spaces or quotes, got %q", c.Prefix))
	}
	if c.Stagger < 0 {
		errs = append(errs, fmt.Errorf("-stagger must not be negative, got %v", c.Stagger))
	}
	if c.Radius < 1 || c.Radius > MaxRadius {
		errs = append(errs, fmt.Errorf("-radius must be in 1..%d, got %d", MaxRadius, c.Radius))
	}
	if c.Walk != WalkCircle && c.Walk != WalkNone {
		errs = append(errs, fmt.Errorf("-walk must be circle or none, got %q", c.Walk))
	}
	if c.SpawnTimeout <= 0 {
		errs = append(errs, fmt.Errorf("-spawn-timeout must be positive, got %v", c.SpawnTimeout))
	}
	if c.JoinRetries < 0 {
		errs = append(errs, fmt.Errorf("-join-retries must not be negative, got %d", c.JoinRetries))
	}
	if c.IdentitySeed == "" {
		errs = append(errs, errors.New("-identity-seed must not be empty"))
	}
	for name, d := range map[string]time.Duration{
		"-progress-interval": c.ProgressInterval, "-stat-interval": c.StatInterval, "-pkt-interval": c.PktInterval,
	} {
		if d < time.Millisecond {
			errs = append(errs, fmt.Errorf("%s must be at least 1ms, got %v", name, d))
		}
	}
	if c.ProbeBots < 0 || c.ProbeBots > c.N {
		errs = append(errs, fmt.Errorf("-probe-bots must be in 0..%d, got %d", c.N, c.ProbeBots))
	}
	if c.DecodeErrors != DecodeCount && c.DecodeErrors != DecodeFail {
		errs = append(errs, fmt.Errorf("-decode-errors must be count or fail, got %q", c.DecodeErrors))
	}
	sort.Slice(errs, func(i, j int) bool { return errs[i].Error() < errs[j].Error() })
	return errors.Join(errs...)
}

// Name is the name of the bot with the given index.
func (c *Config) Name(index int) string {
	return fmt.Sprintf("%s%02d", c.Prefix, index)
}

// Intervals are the reporting intervals announced in the hello event.
func (c *Config) Intervals() Intervals {
	return Intervals{
		Progress: c.ProgressInterval.Milliseconds(),
		Stat:     c.StatInterval.Milliseconds(),
		Pkt:      c.PktInterval.Milliseconds(),
	}
}

// FlagValues returns the effective value of every flag in fs, keyed by name without the dash, for the hello
// event. Durations are rendered as Go duration strings. The output-mode flags are left out.
func FlagValues(fs *flag.FlagSet) map[string]any {
	out := map[string]any{}
	fs.VisitAll(func(f *flag.Flag) {
		if f.Name == "selftest-output" || f.Name == "version" {
			return
		}
		getter, ok := f.Value.(flag.Getter)
		if !ok {
			out[f.Name] = f.Value.String()
			return
		}
		switch v := getter.Get().(type) {
		case time.Duration:
			out[f.Name] = v.String()
		default:
			out[f.Name] = v
		}
	})
	return out
}
