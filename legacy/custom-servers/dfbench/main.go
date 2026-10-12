// Command dfbench is Dragonfly's example server plus a stdin console for the benchmark harness.
package main

import (
	"bufio"
	"fmt"
	"log/slog"
	"math"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/df-mc/dragonfly/server"
	"github.com/df-mc/dragonfly/server/block"
	"github.com/df-mc/dragonfly/server/block/cube"
	"github.com/df-mc/dragonfly/server/entity"
	"github.com/df-mc/dragonfly/server/entity/effect"
	"github.com/df-mc/dragonfly/server/player"
	"github.com/df-mc/dragonfly/server/player/chat"
	"github.com/df-mc/dragonfly/server/session"
	"github.com/df-mc/dragonfly/server/world"
	"github.com/go-gl/mathgl/mgl64"
	"github.com/pelletier/go-toml"
)

func ts() string { return time.Now().Format("15:04:05.000") }

func out(format string, a ...any) { fmt.Printf("[%s] "+format+"\n", append([]any{ts()}, a...)...) }

type loggingListener struct{ server.Listener }

func (l loggingListener) Accept() (session.Conn, error) {
	c, err := l.Listener.Accept()
	if err == nil {
		out("Player connected: %s", c.IdentityData().DisplayName)
	}
	return c, err
}

func main() {
	slog.SetLogLoggerLevel(slog.LevelInfo)
	chat.Global.Subscribe(chat.StdoutSubscriber{})
	uc := server.DefaultConfig()
	data, err := os.ReadFile("config.toml")
	if err != nil {
		panic(err)
	}
	if err := toml.Unmarshal(data, &uc); err != nil {
		panic(err)
	}
	conf, err := uc.Config(slog.Default())
	if err != nil {
		panic(err)
	}
	for i, f := range conf.Listeners {
		f := f
		conf.Listeners[i] = func(c server.Config) (server.Listener, error) {
			l, err := f(c)
			if err != nil {
				return nil, err
			}
			return loggingListener{l}, nil
		}
	}
	tickRange := 4
	if v, err := strconv.Atoi(os.Getenv("DF_TICK_RANGE")); err == nil {
		tickRange = v
	}
	srv := conf.New()
	for _, w := range []*world.World{srv.World(), srv.Nether(), srv.End()} {
		w.SetTickRange(tickRange)
	}
	srv.CloseOnProgramEnd()
	srv.Listen()
	out("Server started. tick-range=%d max-chunk-radius=%d", tickRange, conf.MaxChunkRadius)
	go console(srv)
	for p := range srv.Accept() {
		out("Player spawned: %s", p.Name())
	}
}

func onPlayer(srv *server.Server, name string, f func(tx *world.Tx, p *player.Player)) {
	h, ok := srv.PlayerByName(name)
	if !ok {
		out("No targets matched: %s", name)
		return
	}
	h.Do(func(tx *world.Tx, e world.Entity) {
		if p, ok := e.(*player.Player); ok {
			f(tx, p)
		}
	})
}

func floats(a []string) ([]float64, bool) {
	v := make([]float64, len(a))
	for i, s := range a {
		f, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return nil, false
		}
		v[i] = f
	}
	return v, true
}

var effects = map[string]effect.LastingType{
	"resistance":      effect.Resistance,
	"fire_resistance": effect.FireResistance,
	"water_breathing": effect.WaterBreathing,
	"saturation":      effect.Saturation,
}

var blocks = map[string]world.Block{
	"grass_block": block.Grass{},
	"farmland":    block.Farmland{},
	"oak_leaves":  block.Leaves{Type: block.OakLeaves(), Persistent: true},
	"oak_sapling": block.Sapling{Type: block.OakSapling()},
}

func dimension(srv *server.Server, s string) *world.World {
	switch s {
	case "nether":
		return srv.Nether()
	case "the_end":
		return srv.End()
	}
	return srv.World()
}

func console(srv *server.Server) {
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) == 0 {
			continue
		}
		switch f[0] {
		case "time":
			w := srv.World()
			done := make(chan int, 1)
			w.Do(func(tx *world.Tx) { done <- w.Time() })
			select {
			case t := <-done:
				out("Gametime is %d", t)
			case <-time.After(10 * time.Second):
				out("time query timed out")
			}
		case "tp":
			if len(f) < 5 {
				out("Syntax error: tp <name> x y z")
				continue
			}
			v, ok := floats(f[2:5])
			if !ok {
				out("Syntax error: tp coordinates")
				continue
			}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) { p.Teleport(mgl64.Vec3{v[0], v[1], v[2]}) })
		case "effect":
			if len(f) < 3 {
				continue
			}
			if f[2] == "clear" {
				onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) {
					for _, e := range p.Effects() {
						p.RemoveEffect(e.Type())
					}
				})
				continue
			}
			t, ok := effects[f[2]]
			if !ok {
				out("Invalid effect %s", f[2])
				continue
			}
			lvl := 1
			if len(f) >= 5 {
				if n, err := strconv.Atoi(f[4]); err == nil {
					lvl = min(n+1, 255)
				}
			}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) { p.AddEffect(effect.NewInfinite(t, lvl).WithoutParticles()) })
		case "kill":
			if len(f) < 2 {
				continue
			}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) { p.Hurt(math.MaxFloat32, entity.VoidDamageSource{}) })
		case "kick":
			if len(f) < 2 {
				continue
			}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) { p.Disconnect(strings.Join(f[2:], " ")) })
		case "gamemode":
			if len(f) < 3 {
				continue
			}
			mode := map[string]world.GameMode{"survival": world.GameModeSurvival, "creative": world.GameModeCreative,
				"adventure": world.GameModeAdventure, "spectator": world.GameModeSpectator}[f[1]]
			if mode == nil {
				out("Invalid game mode %s", f[1])
				continue
			}
			onPlayer(srv, f[2], func(tx *world.Tx, p *player.Player) { p.SetGameMode(mode) })
		case "weather":
			if len(f) < 2 {
				continue
			}
			w := srv.World()
			switch f[1] {
			case "clear":
				w.StopThundering()
				w.StopRaining()
			case "rain":
				w.StartRaining(5 * time.Minute)
			case "thunder":
				w.StartThundering(5 * time.Minute)
			}
			out("Changing to %s weather", f[1])
		case "fillnear":
			if len(f) < 9 {
				out("Syntax error: fillnear <name> dx1 dy1 dz1 dx2 dy2 dz2 block")
				continue
			}
			v, ok := floats(f[2:8])
			b, okb := blocks[f[8]]
			if !ok || !okb {
				out("Syntax error: fillnear arguments")
				continue
			}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) {
				o := cube.PosFromVec3(p.Position())
				n := 0
				for x := int(v[0]); x <= int(v[3]); x++ {
					for y := int(v[1]); y <= int(v[4]); y++ {
						for z := int(v[2]); z <= int(v[5]); z++ {
							pos := o.Add(cube.Pos{x, y, z})
							if pos.OutOfBounds(tx.Range()) {
								continue
							}
							tx.SetBlock(pos, b, nil)
							n++
						}
					}
				}
				out("%d blocks filled near %s", n, p.Name())
			})
		case "dim":
			if len(f) < 6 {
				out("Syntax error: dim <name> <overworld|nether|the_end> x y z")
				continue
			}
			v, ok := floats(f[3:6])
			if !ok {
				continue
			}
			dest := dimension(srv, f[2])
			pos := mgl64.Vec3{v[0], v[1], v[2]}
			onPlayer(srv, f[1], func(tx *world.Tx, p *player.Player) {
				if tx.World() == dest {
					p.Teleport(pos)
					return
				}
				h := tx.RemoveEntity(p)
				if h == nil {
					return
				}
				go dest.Do(func(tx *world.Tx) {
					if np, ok := tx.AddEntityAt(h, pos).(*player.Player); ok {
						np.Teleport(pos)
					}
				})
			})
		case "height":
			if len(f) < 3 {
				continue
			}
			v, ok := floats(f[1:3])
			if !ok {
				continue
			}
			w := srv.World()
			done := make(chan string, 1)
			w.Do(func(tx *world.Tx) {
				x, z := int(v[0]), int(v[1])
				y := tx.HighestBlock(x, z)
				b := tx.Block(cube.Pos{x, y, z})
				name, _ := b.EncodeBlock()
				done <- fmt.Sprintf("HEIGHT %d %d y=%d block=%s", x, z, y, name)
			})
			select {
			case s := <-done:
				out("%s", s)
			case <-time.After(20 * time.Second):
				out("height timed out")
			}
		case "list":
			out("There are %d/%d players online", srv.PlayerCount(), srv.MaxPlayerCount())
		case "positions":
			for p := range srv.Players(nil) {
				pos := p.Position()
				out("POS %s %.1f %.1f %.1f", p.Name(), pos[0], pos[1], pos[2])
			}
		case "stop":
			out("Stopping the server")
			_ = srv.Close()
			os.Exit(0)
		default:
			out("Unknown command: %s", f[0])
		}
	}
}
