// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// Control operations read from stdin. "quit" is always accepted as a plain line; the JSON operations need
// -control.
const (
	OpQuit       = "quit"       // stop every bot, write summaries and exit
	OpStop       = "stop"       // JSON form of quit
	OpReconnect  = "reconnect"  // close one bot's session and rejoin after delay_ms (default 3000)
	OpDisconnect = "disconnect" // close one bot's session and stay offline until a reconnect
	OpJitter     = "jitter"     // turn radius jitter on or off
	OpRadius     = "radius"     // request a chunk radius for one bot, or every online bot
)

// DefaultRejoinDelayMS is the pause before a rejoin.
const DefaultRejoinDelayMS = 3000

// ErrEmptyLine is returned for blank input lines, which are ignored.
var ErrEmptyLine = errors.New("empty line")

// Command is one parsed control line. A bot is addressed by name (Bot) or index (I).
type Command struct {
	Op      string `json:"op"`
	Bot     string `json:"bot,omitempty"`
	I       *int   `json:"i,omitempty"`
	On      *bool  `json:"on,omitempty"`
	Radius  int    `json:"radius,omitempty"`
	DelayMS *int   `json:"delay_ms,omitempty"`
}

// Targets reports whether the command addresses a single bot.
func (c Command) Targets() bool { return c.Bot != "" || c.I != nil }

// ParseCommand parses one stdin line. control says whether -control is set.
func ParseCommand(line string, control bool) (Command, error) {
	line = strings.TrimSpace(line)
	if line == "" {
		return Command{}, ErrEmptyLine
	}
	if line == OpQuit {
		return Command{Op: OpQuit}, nil
	}
	if !control {
		return Command{}, fmt.Errorf("only %q is accepted without -control: %q", OpQuit, line)
	}
	dec := json.NewDecoder(bytes.NewReader([]byte(line)))
	dec.DisallowUnknownFields()
	var c Command
	if err := dec.Decode(&c); err != nil {
		return Command{}, fmt.Errorf("bad control line %q: %w", line, err)
	}
	if dec.More() {
		return Command{}, fmt.Errorf("bad control line %q: trailing data", line)
	}
	if c.I != nil && *c.I < 0 {
		return Command{}, fmt.Errorf("bad control line %q: negative bot index", line)
	}
	if c.DelayMS != nil && *c.DelayMS < 0 {
		return Command{}, fmt.Errorf("bad control line %q: negative delay_ms", line)
	}
	switch c.Op {
	case OpQuit, OpStop:
		if c.Targets() || c.On != nil || c.Radius != 0 || c.DelayMS != nil {
			return Command{}, fmt.Errorf("bad control line %q: %s takes no arguments", line, c.Op)
		}
		c.Op = OpQuit
	case OpReconnect, OpDisconnect:
		if !c.Targets() {
			return Command{}, fmt.Errorf("bad control line %q: %s needs bot or i", line, c.Op)
		}
		if c.Op == OpDisconnect && c.DelayMS != nil {
			return Command{}, fmt.Errorf("bad control line %q: disconnect takes no delay_ms", line)
		}
	case OpJitter:
		if c.On == nil {
			return Command{}, fmt.Errorf("bad control line %q: jitter needs on", line)
		}
	case OpRadius:
		if c.Radius < 1 || c.Radius > MaxRadius {
			return Command{}, fmt.Errorf("bad control line %q: radius needs radius in 1..%d", line, MaxRadius)
		}
	default:
		return Command{}, fmt.Errorf("bad control line %q: unknown op %q", line, c.Op)
	}
	return c, nil
}
