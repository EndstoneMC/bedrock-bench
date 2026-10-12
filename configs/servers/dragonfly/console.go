// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/df-mc/dragonfly/server"
	"github.com/df-mc/dragonfly/server/entity/effect"
	"github.com/df-mc/dragonfly/server/player"
	"github.com/df-mc/dragonfly/server/world"
	"github.com/go-gl/mathgl/mgl64"
)

// The console reads one command per line from standard input; commands below, replies in README.md.
// Commands that touch the world run as tasks on the world (or player) that owns the data, so a slow tick delays the
// reply but never the console. A reply is logged at info level; a command that fails says why at error level. A
// target is a player name, in any case, or @a for every player; names with spaces are written in double quotes.

type command struct {
	usage   string
	minArgs int
	maxArgs int // -1: no limit
	run     func(c *console, args []string) error
}

var commands map[string]command

func init() {
	commands = map[string]command{
		"gametime":  {"gametime", 0, 0, (*console).gametime},
		"list":      {"list", 0, 0, (*console).list},
		"positions": {"positions", 0, 0, (*console).positions},
		"tp":        {"tp <target> <x> <y> <z>", 4, 4, (*console).tp},
		"effect":    {"effect <target> <effect> [seconds|infinite] [amplifier] [hideParticles]", 2, 5, (*console).effect},
		"help":      {"help", 0, 0, (*console).help},
		"stop":      {"stop", 0, 0, (*console).stop},
	}
}

// effects are the effects the console can give, by their vanilla command names.
var effects = map[string]effect.LastingType{
	"fire_resistance": effect.FireResistance,
	"resistance":      effect.Resistance,
	"saturation":      effect.Saturation,
	"water_breathing": effect.WaterBreathing,
}

// defaultEffectSeconds is the effect duration when none is given, as for the vanilla effect command.
const defaultEffectSeconds = 30

var errUsage = errors.New("usage")

type console struct {
	srv     *server.Server
	players *roster
	stopped sync.Once
}

func newConsole(srv *server.Server, players *roster) *console {
	return &console{srv: srv, players: players}
}

func reply(format string, a ...any) { slog.Info(fmt.Sprintf(format, a...)) }

func fail(format string, a ...any) { slog.Error(fmt.Sprintf(format, a...)) }

// run executes commands until r ends, then stops the server: a harness that goes away takes the server with it.
func (c *console) run(r io.Reader) {
	scanner := bufio.NewScanner(r)
	for scanner.Scan() {
		c.exec(scanner.Text())
	}
	reply("Console input closed")
	_ = c.stop(nil)
}

func (c *console) exec(line string) {
	args, err := fields(line)
	if err != nil {
		fail("%v", err)
		return
	}
	if len(args) == 0 {
		return
	}
	name := strings.ToLower(strings.TrimPrefix(args[0], "/"))
	cmd, ok := commands[name]
	if !ok {
		fail("Unknown command: %s. Type help for a list of commands.", args[0])
		return
	}
	args = args[1:]
	if len(args) < cmd.minArgs || (cmd.maxArgs >= 0 && len(args) > cmd.maxArgs) {
		fail("Usage: %s", cmd.usage)
		return
	}
	if err := cmd.run(c, args); err != nil {
		if errors.Is(err, errUsage) {
			err = fmt.Errorf("Usage: %s", cmd.usage)
		}
		fail("%v", err)
	}
}

// fields splits a command line at spaces. A double-quoted part is one field, so "a player" names a player whose name
// has a space.
func fields(line string) ([]string, error) {
	var (
		out     []string
		cur     strings.Builder
		quoted  bool
		started bool
	)
	for _, r := range line {
		switch {
		case r == '"':
			quoted = !quoted
			started = true
		case (r == ' ' || r == '\t' || r == '\r') && !quoted:
			if started {
				out = append(out, cur.String())
				cur.Reset()
				started = false
			}
		default:
			cur.WriteRune(r)
			started = true
		}
	}
	if quoted {
		return nil, errors.New("Syntax error: unterminated quote")
	}
	if started {
		out = append(out, cur.String())
	}
	return out, nil
}

// worlds are the server's worlds, the overworld first.
func (c *console) worlds() []*world.World {
	return []*world.World{c.srv.World(), c.srv.Nether(), c.srv.End()}
}

// inEachWorld runs f in a task on every world and calls done once all of them have finished.
func (c *console) inEachWorld(f func(tx *world.Tx), done func()) {
	var wg sync.WaitGroup
	for _, w := range c.worlds() {
		wg.Add(1)
		w.Do(f).OnDone(func(error) { wg.Done() })
	}
	go func() {
		wg.Wait()
		done()
	}()
}

// each runs f for every player the target names, each in a task on the player's world, then calls done with the
// number of players f ran for.
func (c *console) each(target string, f func(tx *world.Tx, p *player.Player), done func(n int)) error {
	if target == "@a" {
		var n atomic.Int64
		c.inEachWorld(func(tx *world.Tx) {
			// Collect first: f may move a player out of this world.
			var found []*player.Player
			for e := range tx.Players() {
				if p, ok := e.(*player.Player); ok {
					found = append(found, p)
				}
			}
			for _, p := range found {
				f(tx, p)
			}
			n.Add(int64(len(found)))
		}, func() { done(int(n.Load())) })
		return nil
	}
	h, ok := c.players.get(target)
	if !ok {
		return fmt.Errorf("No targets matched: %s", target)
	}
	player.Do(h, func(tx *world.Tx, p *player.Player) {
		f(tx, p)
		done(1)
	}).OnDone(func(err error) {
		if err != nil {
			fail("No targets matched: %s (%v)", target, err)
		}
	})
	return nil
}

// who names the players a reply is about.
func who(target string, n int) string {
	if target == "@a" {
		return fmt.Sprintf("%d players", n)
	}
	return target
}

func (c *console) gametime([]string) error {
	c.srv.World().Do(func(tx *world.Tx) {
		reply("Gametime is %d", tx.CurrentTick())
	})
	return nil
}

func (c *console) list([]string) error {
	names := c.players.names()
	reply("There are %d/%d players online:", len(names), c.srv.MaxPlayerCount())
	if len(names) > 0 {
		reply("%s", strings.Join(names, ", "))
	}
	return nil
}

func (c *console) positions([]string) error {
	type position struct {
		name string
		dim  int
		pos  mgl64.Vec3
	}
	var (
		mu  sync.Mutex
		all []position
	)
	c.inEachWorld(func(tx *world.Tx) {
		dim, _ := world.DimensionID(tx.World().Dimension())
		for e := range tx.Players() {
			if p, ok := e.(*player.Player); ok {
				mu.Lock()
				all = append(all, position{p.Name(), dim, p.Position()})
				mu.Unlock()
			}
		}
	}, func() {
		slices.SortFunc(all, func(a, b position) int { return strings.Compare(a.name, b.name) })
		reply("Positions: %d", len(all))
		for _, p := range all {
			reply("Position: %d %.2f %.2f %.2f %s", p.dim, p.pos[0], p.pos[1], p.pos[2], p.name)
		}
	})
	return nil
}

func (c *console) tp(args []string) error {
	pos, err := vec3(args[1:4])
	if err != nil {
		return err
	}
	return c.each(args[0], func(_ *world.Tx, p *player.Player) {
		p.Teleport(pos)
	}, func(n int) {
		reply("Teleported %s to %s", who(args[0], n), formatVec3(pos))
	})
}

func (c *console) effect(args []string) error {
	target, name := args[0], strings.TrimPrefix(args[1], "minecraft:")
	kind, ok := effects[name]
	if !ok {
		return fmt.Errorf("Unknown effect: %s", args[1])
	}
	e, err := parseEffect(kind, args[2:])
	if err != nil {
		return err
	}
	return c.each(target, func(_ *world.Tx, p *player.Player) {
		p.AddEffect(e)
	}, func(n int) {
		reply("Gave %s %d to %s", name, e.Level(), who(target, n))
	})
}

// parseEffect reads "[seconds|infinite] [amplifier] [hideParticles]" like the vanilla effect command. The level is
// the amplifier plus one, at most 255.
func parseEffect(kind effect.LastingType, args []string) (effect.Effect, error) {
	seconds, infinite := defaultEffectSeconds, false
	if len(args) > 0 {
		if args[0] == "infinite" {
			infinite = true
		} else {
			n, err := strconv.Atoi(args[0])
			if err != nil || n <= 0 {
				return effect.Effect{}, fmt.Errorf("Invalid duration: %s", args[0])
			}
			seconds = n
		}
	}
	level := 1
	if len(args) > 1 {
		amplifier, err := strconv.Atoi(args[1])
		if err != nil || amplifier < 0 {
			return effect.Effect{}, fmt.Errorf("Invalid amplifier: %s", args[1])
		}
		level = min(amplifier+1, 255)
	}
	hide := false
	if len(args) > 2 {
		var err error
		if hide, err = strconv.ParseBool(args[2]); err != nil {
			return effect.Effect{}, fmt.Errorf("Invalid hideParticles value: %s", args[2])
		}
	}
	e := effect.New(kind, level, time.Duration(seconds)*time.Second)
	if infinite {
		e = effect.NewInfinite(kind, level)
	}
	if hide {
		e = e.WithoutParticles()
	}
	return e, nil
}

func (c *console) help([]string) error {
	names := make([]string, 0, len(commands))
	for name := range commands {
		names = append(names, name)
	}
	slices.Sort(names)
	for _, name := range names {
		reply("%s", commands[name].usage)
	}
	return nil
}

func (c *console) stop([]string) error {
	c.stopped.Do(func() {
		reply("Stopping the server")
		if err := c.srv.Close(); err != nil {
			fail("Stopping the server failed: %v", err)
		}
	})
	return nil
}

// vec3 parses three coordinates.
func vec3(args []string) (mgl64.Vec3, error) {
	var v mgl64.Vec3
	for i, s := range args {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return v, fmt.Errorf("Invalid coordinate: %s", s)
		}
		v[i] = f
	}
	return v, nil
}

func formatVec3(v mgl64.Vec3) string {
	return strconv.FormatFloat(v[0], 'f', -1, 64) + " " + strconv.FormatFloat(v[1], 'f', -1, 64) + " " +
		strconv.FormatFloat(v[2], 'f', -1, 64)
}

// roster finds online players by name, case-insensitively. Dragonfly's own lookup by name reads the player map
// without its lock, so the console keeps its own.
type roster struct {
	mu     sync.Mutex
	byName map[string]rosterEntry
}

type rosterEntry struct {
	name   string
	handle *world.EntityHandle
}

func newRoster() *roster {
	return &roster{byName: make(map[string]rosterEntry)}
}

func (r *roster) add(name string, h *world.EntityHandle) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byName[strings.ToLower(name)] = rosterEntry{name, h}
}

// remove forgets a player, unless the name already belongs to a newer session.
func (r *roster) remove(name string, h *world.EntityHandle) {
	r.mu.Lock()
	defer r.mu.Unlock()
	key := strings.ToLower(name)
	if entry, ok := r.byName[key]; ok && entry.handle == h {
		delete(r.byName, key)
	}
}

func (r *roster) get(name string) (*world.EntityHandle, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	entry, ok := r.byName[strings.ToLower(name)]
	return entry.handle, ok
}

// names returns the online players' names, sorted.
func (r *roster) names() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, 0, len(r.byName))
	for _, entry := range r.byName {
		out = append(out, entry.name)
	}
	slices.Sort(out)
	return out
}
