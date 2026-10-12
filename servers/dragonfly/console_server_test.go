// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"context"
	"io"
	"log"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/df-mc/dragonfly/server"
	"github.com/df-mc/dragonfly/server/block"
	"github.com/df-mc/dragonfly/server/block/cube"
	"github.com/df-mc/dragonfly/server/entity/effect"
	"github.com/df-mc/dragonfly/server/player"
	"github.com/df-mc/dragonfly/server/world"
	"github.com/go-gl/mathgl/mgl64"
)

// syncBuffer collects log output written from several goroutines.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// consoleTest runs console commands against a Dragonfly server with in-memory worlds and no network listener. Its one
// player has no client: it exists only in the world.
type consoleTest struct {
	t       *testing.T
	srv     *server.Server
	console *console
	player  *world.EntityHandle
	out     *syncBuffer
}

func newConsoleTest(t *testing.T) *consoleTest {
	t.Helper()
	if testing.Short() {
		t.Skip("runs a Dragonfly server in memory")
	}
	out := &syncBuffer{}
	flags, writer := log.Flags(), log.Writer()
	log.SetFlags(0)
	log.SetOutput(out)
	t.Cleanup(func() {
		log.SetFlags(flags)
		log.SetOutput(writer)
	})

	srv := server.Config{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), MaxPlayers: 10}.New()
	srv.Listen()
	keepTicking(srv.World())
	players := newRoster()
	h := world.EntitySpawnOpts{Position: mgl64.Vec3{0, 10, 0}}.New(player.Type,
		player.Config{Name: "a00", GameMode: world.GameModeSurvival})
	if _, err := world.Call(context.Background(), srv.World(), func(tx *world.Tx) (bool, error) {
		tx.AddEntity(h)
		return true, nil
	}); err != nil {
		t.Fatal(err)
	}
	players.add("a00", h)
	ct := &consoleTest{t: t, srv: srv, console: newConsole(srv, players), player: h, out: out}
	t.Cleanup(func() { _ = ct.console.stop(nil) })
	return ct
}

// run executes a command and waits until, for each wanted prefix, a line starting with it has been logged.
func (ct *consoleTest) run(command string, want ...string) {
	ct.t.Helper()
	start := len(ct.out.String())
	ct.console.exec(command)
	deadline := time.Now().Add(5 * time.Second)
	for {
		got := ct.out.String()[start:]
		missing := ""
		for _, w := range want {
			if !strings.HasPrefix(got, w) && !strings.Contains(got, "\n"+w) {
				missing = w
				break
			}
		}
		if missing == "" {
			return
		}
		if time.Now().After(deadline) {
			ct.t.Fatalf("%q: no line %q in:\n%s", command, missing, got)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// inspect runs f with the test player in its world and waits for it.
func (ct *consoleTest) inspect(f func(tx *world.Tx, p *player.Player)) {
	ct.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := player.Call(ctx, ct.player, func(tx *world.Tx, p *player.Player) (bool, error) {
		f(tx, p)
		return true, nil
	}); err != nil {
		ct.t.Fatal(err)
	}
}

func TestConsoleAgainstAServer(t *testing.T) {
	ct := newConsoleTest(t)

	ct.run("gametime", "INFO Gametime is")
	ct.run("list", "INFO There are 1/10 players online:", "INFO a00")

	ct.run("tp A00 100.5 64 100.5", "INFO Teleported A00 to 100.5 64 100.5")
	ct.inspect(func(_ *world.Tx, p *player.Player) {
		if pos := p.Position(); pos != (mgl64.Vec3{100.5, 64, 100.5}) {
			t.Errorf("position after tp = %v", pos)
		}
	})
	ct.run("positions", "INFO Positions: 1", "INFO Position: 0 100.50 64.00 100.50 a00")

	ct.run("effect a00 resistance infinite 255 true", "INFO Gave resistance 255 to a00")
	ct.run("effect @a saturation infinite 255 true", "INFO Gave saturation 255 to 1 players")
	ct.inspect(func(_ *world.Tx, p *player.Player) {
		e, ok := p.Effect(effect.Resistance)
		if !ok || e.Level() != 255 || !e.Infinite() || !e.ParticlesHidden() {
			t.Errorf("resistance = %+v, %v", e, ok)
		}
	})
	ct.run("effect a00 clear", "INFO Took all effects from a00")
	ct.inspect(func(_ *world.Tx, p *player.Player) {
		if n := len(p.Effects()); n != 0 {
			t.Errorf("%d effects left after clear", n)
		}
	})

	ct.run("gamemode adventure a00", "INFO Set the game mode of a00 to adventure")
	ct.inspect(func(_ *world.Tx, p *player.Player) {
		if p.GameMode() != world.GameModeAdventure {
			t.Errorf("game mode = %v", p.GameMode())
		}
	})

	ct.run("fillnear a00 0 -1 0 1 -1 1 stone", "INFO 4 blocks filled near a00")
	ct.inspect(func(tx *world.Tx, _ *player.Player) {
		if b := tx.Block(cube.Pos{101, 63, 101}); b != (block.Stone{}) {
			t.Errorf("block after fillnear = %#v", b)
		}
	})

	ct.run("weather thunder 60", "INFO Changed the weather to thunder")
	ct.run("weather clear", "INFO Changed the weather to clear")

	ct.run("tpdim a00 nether 10 70 10", "INFO Teleported a00 to 10 70 10 in nether")
	ct.run("positions", "INFO Position: 1 10.00 70.00 10.00 a00")

	ct.run("kill a00", "INFO Killed a00")
	ct.inspect(func(_ *world.Tx, p *player.Player) {
		if !p.Dead() {
			t.Error("a00 is alive after kill")
		}
	})
	ct.run("kill a00", "INFO a00 is already dead")
	ct.run("kick a00 bye", "INFO Kicked a00")
	ct.run("tp ghost 0 0 0", "ERROR No targets matched: ghost")
	ct.run("help", "INFO stop")
	ct.run("stop", "INFO Stopping the server")
}
