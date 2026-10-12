// Command loadbot joins N offline-mode bots to a BDS server and keeps them walking.
package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"math"
	"math/rand"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/go-gl/mathgl/mgl32"
	"github.com/google/uuid"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/login"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

var (
	joined      atomic.Int64
	failed      atomic.Int64
	dropped     atomic.Int64
	rxPackets   atomic.Int64
	txPackets   atomic.Int64
	corrections atomic.Int64
	chunks      atomic.Int64
	subchunks   atomic.Int64
	tpBots      atomic.Int64
	uniqChunks  atomic.Int64

	rxByID   [1024]atomic.Int64
	idNameMu sync.Mutex
	idName   = map[uint32]string{}
	lastByID [1024]int64
	lastPkt  time.Time
	deaths   atomic.Int64

	botMu    sync.Mutex
	botStats = map[string]*botStat{}

	probeEvery time.Duration
	probeKind  string
	loadScreen bool
	rttMu      sync.Mutex
	rtts       []time.Duration
	probeLost  atomic.Int64

	left     atomic.Int64
	rejoins  atomic.Int64
	respawns atomic.Int64
	dimDone  atomic.Int64
	ctlMu    sync.Mutex
	ctl      = map[string]chan string{}
	control  bool

	rejoinOnDrop bool
	jitterOn     atomic.Bool
	radiusJitter time.Duration
)

var (
	cmdLines   string
	cmdDelay   time.Duration
	cmdBots    int
	cmdVersion string
)

// botIndex is the trailing number of a bot name.
func botIndex(name string) int {
	i := len(name)
	for i > 0 && name[i-1] >= '0' && name[i-1] <= '9' {
		i--
	}
	n := 0
	for _, c := range name[i:] {
		n = n*10 + int(c-'0')
	}
	return n
}

// readControl takes "reconnect <name>" lines from stdin.
func readControl() {
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) < 2 {
			continue
		}
		if f[0] == "jitter" {
			jitterOn.Store(f[1] == "on")
			fmt.Printf("%s CTL jitter %s\n", ts(), f[1])
			continue
		}
		ctlMu.Lock()
		ch := ctl[f[1]]
		ctlMu.Unlock()
		if ch == nil {
			fmt.Printf("%s CTL unknown %s\n", ts(), f[1])
			continue
		}
		select {
		case ch <- f[0]:
		default:
			fmt.Printf("%s CTL busy %s\n", ts(), f[1])
		}
	}
}

type botStat struct {
	chunks, chunksAfterTP, subAfterTP, corrections, teleports int64
	lastPos                                                   mgl32.Vec3
	uniq                                                      map[[2]int32]struct{}
}

func addRTT(d time.Duration) {
	rttMu.Lock()
	rtts = append(rtts, d)
	rttMu.Unlock()
}

func rttSummary() string {
	rttMu.Lock()
	v := append([]time.Duration(nil), rtts...)
	rttMu.Unlock()
	if len(v) == 0 {
		return "rtt n=0"
	}
	sort.Slice(v, func(i, j int) bool { return v[i] < v[j] })
	var sum time.Duration
	for _, x := range v {
		sum += x
	}
	q := func(f float64) float64 { return float64(v[int(f*float64(len(v)-1))].Microseconds()) / 1000 }
	return fmt.Sprintf("rtt n=%d mean=%.2fms p50=%.2fms p90=%.2fms p99=%.2fms max=%.2fms lost=%d", len(v),
		float64(sum.Microseconds())/1000/float64(len(v)), q(0.5), q(0.9), q(0.99), q(1), probeLost.Load())
}

func main() {
	addr := flag.String("addr", "127.0.0.1:19632", "server address")
	n := flag.Int("n", 10, "number of bots")
	dur := flag.Duration("d", 60*time.Second, "time to keep bots online after all joined")
	stagger := flag.Duration("stagger", 300*time.Millisecond, "delay between joins")
	settle := flag.Duration("settle", 0, "wait after all joined before measuring")
	prefix := flag.String("prefix", "bot", "name prefix")
	flag.DurationVar(&probeEvery, "probe", 0, "latency probe interval per bot (0 = off)")
	flag.StringVar(&probeKind, "probekind", "radius", "radius or nsl")
	flag.BoolVar(&loadScreen, "loadscreen", true, "send ServerBoundLoadingScreen start/end after spawn")
	radius := flag.Int("radius", 16, "chunk radius requested at spawn")
	flag.BoolVar(&csg, "csg", false, "report hardware capable of client-side chunk generation")
	flag.BoolVar(&control, "control", false, "read \"reconnect <name>\" commands from stdin")
	flag.BoolVar(&rejoinOnDrop, "rejoin-on-drop", false, "reconnect a bot that was kicked or dropped")
	flag.DurationVar(&radiusJitter, "radius-jitter", 0, "send RequestChunkRadius 4-16 about this often per bot (0 = off)")
	flag.StringVar(&cmdLines, "cmd", "", "commands (';'-separated) each of the first -cmd-bots bots sends after spawn")
	flag.DurationVar(&cmdDelay, "cmd-delay", 8*time.Second, "wait after spawn before sending -cmd")
	flag.IntVar(&cmdBots, "cmd-bots", 1, "how many bots send -cmd")
	flag.StringVar(&cmdVersion, "cmd-version", "latest", "CommandRequest version string")
	jitterPaused := flag.Bool("jitter-paused", false, "start with radius jitter paused until \"jitter on\" arrives on stdin")
	flag.Parse()
	minecraft.RequestRadius = int32(*radius)
	reqRadius = int32(*radius)
	jitterOn.Store(!*jitterPaused)
	if control {
		go readControl()
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()

	var wg sync.WaitGroup
	ready := make(chan struct{}, *n)
	stop := make(chan struct{})
	for i := 0; i < *n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			runBot(ctx, *addr, fmt.Sprintf("%s%02d", *prefix, i), ready, stop)
		}(i)
		select {
		case <-time.After(*stagger):
		case <-ctx.Done():
		}
	}

	start := time.Now()
	for joined.Load()+failed.Load() < int64(*n) && time.Since(start) < 3*time.Minute && ctx.Err() == nil {
		time.Sleep(200 * time.Millisecond)
	}
	fmt.Printf("%s ALLJOINED joined=%d failed=%d\n", ts(), joined.Load(), failed.Load())
	time.Sleep(*settle)
	rttMu.Lock()
	rtts = nil
	rttMu.Unlock()
	probeLost.Store(0)
	fmt.Printf("%s MEASURE\n", ts())

	t0 := time.Now()
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()
	var lastRx, lastTx int64
	statN := 0
loop:
	for time.Since(t0) < *dur {
		select {
		case <-ctx.Done():
			break loop
		case <-ticker.C:
			rx, tx := rxPackets.Load(), txPackets.Load()
			fmt.Printf("%s STAT online=%d dropped=%d tp=%d deaths=%d rx/s=%.0f tx/s=%.0f corrections=%d chunks=%d subchunks=%d uniq=%d left=%d rejoins=%d respawns=%d dims=%d\n", ts(),
				joined.Load()+rejoins.Load()-dropped.Load()-left.Load(), dropped.Load(), tpBots.Load(), deaths.Load(), float64(rx-lastRx)/10, float64(tx-lastTx)/10, corrections.Load(), chunks.Load(), subchunks.Load(), uniqChunks.Load(),
				left.Load(), rejoins.Load(), respawns.Load(), dimDone.Load())
			lastRx, lastTx = rx, tx
			statN++
			if statN == 1 {
				dumpPackets()
			}
			if statN%6 == 0 {
				dumpBots()
				dumpPackets()
			}
		case <-time.After(time.Until(t0.Add(*dur))):
			break loop
		}
	}
	fmt.Printf("%s %s\n", ts(), rttSummary())
	fmt.Printf("%s DONE joined=%d failed=%d dropped=%d rx=%d tx=%d corrections=%d chunks=%d\n", ts(),
		joined.Load(), failed.Load(), dropped.Load(), rxPackets.Load(), txPackets.Load(), corrections.Load(), chunks.Load())
	dumpBots()
	close(stop)
	wg.Wait()
}

func dumpPackets() {
	now := time.Now()
	dt := now.Sub(lastPkt).Seconds()
	n := float64(joined.Load() - dropped.Load())
	type row struct {
		id   uint32
		rate float64
		tot  int64
	}
	var rows []row
	for id := range rxByID {
		c := rxByID[id].Load()
		if c > lastByID[id] && !lastPkt.IsZero() {
			rows = append(rows, row{uint32(id), float64(c-lastByID[id]) / dt, c})
		}
		lastByID[id] = c
	}
	lastPkt = now
	sort.Slice(rows, func(i, j int) bool { return rows[i].rate > rows[j].rate })
	idNameMu.Lock()
	defer idNameMu.Unlock()
	for _, r := range rows {
		fmt.Printf("%s PKT id=%d name=%s rate/s=%.1f per-bot/s=%.2f\n", ts(), r.id, idName[r.id], r.rate, r.rate/n)
	}
}

func dumpBots() {
	botMu.Lock()
	names := make([]string, 0, len(botStats))
	for k := range botStats {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, k := range names {
		b := botStats[k]
		fmt.Printf("%s BOT %s chunks=%d chunksAfterTP=%d subAfterTP=%d corrections=%d teleports=%d uniq=%d pos=%.1f,%.1f,%.1f\n", ts(), k,
			b.chunks, b.chunksAfterTP, b.subAfterTP, b.corrections, b.teleports, len(b.uniq), b.lastPos[0], b.lastPos[1], b.lastPos[2])
	}
	botMu.Unlock()
}

var (
	csg       bool
	reqRadius int32 = 16
)

func memTier() int {
	if csg {
		return 4
	}
	return 0
}

func ts() string { return time.Now().Format("15:04:05.000") }

func runBot(ctx context.Context, addr, name string, ready chan<- struct{}, stop <-chan struct{}) {
	ch := make(chan string, 1)
	ctlMu.Lock()
	ctl[name] = ch
	ctlMu.Unlock()
	id := uuid.NewString()
	rejoin, fails := false, 0
	for {
		again, ok := session(ctx, addr, name, id, rejoin, ready, stop, ch)
		if !again {
			return
		}
		if ok {
			fails = 0
		} else if fails++; fails >= 5 {
			fmt.Printf("%s %s REJOIN GAVE UP\n", ts(), name)
			return
		}
		rejoin = true
		select {
		case <-stop:
			return
		case <-ctx.Done():
			return
		case <-time.After(3 * time.Second):
		}
	}
}

// session runs one connection; again reports whether to reconnect, ok whether the bot spawned.
func session(ctx context.Context, addr, name, id string, rejoin bool, ready chan<- struct{}, stop <-chan struct{}, ch <-chan string) (again, ok bool) {
	dialer := minecraft.Dialer{
		IdentityData:         login.IdentityData{DisplayName: name, Identity: id},
		EnableClientCache:    true,
		ClientData:           login.ClientData{CompatibleWithClientSideChunkGen: csg, MaxViewDistance: 96, MemoryTier: memTier()},
		DownloadResourcePack: func(uuid.UUID, string, int, int) bool { return false },
	}
	dctx, dcancel := context.WithTimeout(ctx, 60*time.Second)
	defer dcancel()
	conn, err := dialer.DialContext(dctx, "raknet", addr)
	if err != nil {
		if rejoin {
			fmt.Printf("%s %s REJOIN FAILED: %v\n", ts(), name, err)
			return true, false
		}
		failed.Add(1)
		fmt.Printf("%s %s DIAL FAILED: %v\n", ts(), name, err)
		return false, false
	}
	defer conn.Close()
	if err := conn.DoSpawnContext(dctx); err != nil {
		if rejoin {
			fmt.Printf("%s %s REJOIN FAILED: %v\n", ts(), name, err)
			return true, false
		}
		failed.Add(1)
		fmt.Printf("%s %s SPAWN FAILED: %v\n", ts(), name, err)
		return false, false
	}
	if rejoin {
		rejoins.Add(1)
	} else {
		joined.Add(1)
		select {
		case ready <- struct{}{}:
		default:
		}
	}

	if loadScreen {
		_ = conn.WritePacket(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeStart})
		_ = conn.WritePacket(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeEnd})
		_ = conn.Flush()
	}
	if cmdLines != "" && botIndex(name) < cmdBots {
		go func() {
			select {
			case <-stop:
				return
			case <-time.After(cmdDelay):
			}
			for _, line := range strings.Split(cmdLines, ";") {
				if line = strings.TrimSpace(line); line == "" {
					continue
				}
				_ = conn.WritePacket(&packet.CommandRequest{
					CommandLine:   line,
					CommandOrigin: protocol.CommandOrigin{Origin: protocol.CommandOriginPlayer, UUID: uuid.New()},
					Version:       cmdVersion,
				})
				_ = conn.Flush()
				fmt.Printf("%s CMD %s %s\n", ts(), name, line)
				time.Sleep(time.Second)
			}
		}()
	}
	gd := conn.GameData()
	st := &botStat{uniq: map[[2]int32]struct{}{}}
	botMu.Lock()
	botStats[name] = st
	botMu.Unlock()
	fmt.Printf("%s JOINED %s %.1f %.1f %.1f radius=%d rejoin=%t\n", ts(), name, gd.PlayerPosition[0], gd.PlayerPosition[1], gd.PlayerPosition[2], gd.ChunkRadius, rejoin)
	var mu sync.Mutex
	pos := gd.PlayerPosition
	var pendMu sync.Mutex
	var pendAt time.Time
	var pendTS int64
	var teleported, leaving, dead, readySent atomic.Bool
	done := make(chan struct{})

	var sendRespawn func(try int)
	sendRespawn = func(try int) {
		if !dead.Load() {
			return
		}
		readySent.Store(true)
		_ = conn.WritePacket(&packet.Respawn{State: packet.RespawnStateClientReadyToSpawn, EntityRuntimeID: gd.EntityRuntimeID})
		_ = conn.WritePacket(&packet.PlayerAction{EntityRuntimeID: gd.EntityRuntimeID, ActionType: protocol.PlayerActionRespawn, BlockFace: -1})
		_ = conn.Flush()
		if try < 3 {
			time.AfterFunc(10*time.Second, func() { sendRespawn(try + 1) })
		}
	}
	markDead := func(why string) {
		if dead.CompareAndSwap(false, true) {
			deaths.Add(1)
			fmt.Printf("%s DIED %s %s\n", ts(), name, why)
			time.AfterFunc(time.Second, func() { sendRespawn(0) })
		}
	}
	markAlive := func(p mgl32.Vec3, why string) {
		if readySent.Load() && dead.CompareAndSwap(true, false) {
			readySent.Store(false)
			respawns.Add(1)
			fmt.Printf("%s RESPAWNED %s %.1f %.1f %.1f %s\n", ts(), name, p[0], p[1], p[2], why)
		}
	}

	go func() {
		defer close(done)
		for {
			pk, err := conn.ReadPacket()
			if err != nil {
				select {
				case <-stop:
				default:
					if !leaving.Load() {
						dropped.Add(1)
						fmt.Printf("%s %s READ FAILED: %v\n", ts(), name, err)
					}
				}
				return
			}
			rxPackets.Add(1)
			if id := pk.ID(); id < 1024 {
				if rxByID[id].Add(1) == 1 {
					idNameMu.Lock()
					idName[id] = fmt.Sprintf("%T", pk)
					idNameMu.Unlock()
				}
			}
			switch p := pk.(type) {
			case *packet.LevelChunk:
				chunks.Add(1)
				botMu.Lock()
				st.chunks++
				if st.teleports > 0 {
					st.chunksAfterTP++
					if _, ok := st.uniq[[2]int32{p.Position[0], p.Position[1]}]; !ok {
						st.uniq[[2]int32{p.Position[0], p.Position[1]}] = struct{}{}
						uniqChunks.Add(1)
					}
				}
				botMu.Unlock()
				if len(p.BlobHashes) > 0 {
					_ = conn.WritePacket(&packet.ClientCacheBlobStatus{MissHashes: p.BlobHashes})
					txPackets.Add(1)
				}
				if lim, ok := p.SubChunkLimit.Value(); ok {
					_ = conn.WritePacket(&packet.SubChunkRequest{
						Dimension: p.Dimension,
						Offsets:   column(lim),
						Position:  protocol.SubChunkPos{p.Position[0], 0, p.Position[1]},
					})
					txPackets.Add(1)
				}
			case *packet.SubChunk:
				subchunks.Add(int64(len(p.SubChunkEntries)))
				botMu.Lock()
				if st.teleports > 0 {
					st.subAfterTP += int64(len(p.SubChunkEntries))
				}
				botMu.Unlock()
				var miss []uint64
				for _, e := range p.SubChunkEntries {
					if h, ok := e.BlobHash.Value(); ok {
						miss = append(miss, h)
					}
				}
				if len(miss) > 0 {
					_ = conn.WritePacket(&packet.ClientCacheBlobStatus{MissHashes: miss})
					txPackets.Add(1)
				}
			case *packet.ChunkRadiusUpdated:
				if probeKind == "radius" {
					pendMu.Lock()
					if !pendAt.IsZero() {
						addRTT(time.Since(pendAt))
						pendAt = time.Time{}
					}
					pendMu.Unlock()
				}
			case *packet.NetworkStackLatency:
				pendMu.Lock()
				if probeKind == "nsl" && !pendAt.IsZero() && (p.Timestamp == pendTS || p.Timestamp == pendTS*1000 || p.Timestamp*1000 == pendTS) {
					addRTT(time.Since(pendAt))
					pendAt = time.Time{}
					pendMu.Unlock()
					break
				}
				pendMu.Unlock()
				if p.NeedsResponse {
					_ = conn.WritePacket(&packet.NetworkStackLatency{Timestamp: p.Timestamp})
					txPackets.Add(1)
				}
			case *packet.CorrectPlayerMovePrediction:
				corrections.Add(1)
				mu.Lock()
				pos = p.Position
				mu.Unlock()
				botMu.Lock()
				st.corrections++
				st.lastPos = p.Position
				botMu.Unlock()
			case *packet.SetHealth:
				mu.Lock()
				cur := pos
				mu.Unlock()
				if p.Health <= 0 {
					markDead("sethealth")
				} else {
					markAlive(cur, "sethealth")
				}
			case *packet.UpdateAttributes:
				if p.EntityRuntimeID == gd.EntityRuntimeID {
					for _, a := range p.Attributes {
						if a.Name != "minecraft:health" {
							continue
						}
						mu.Lock()
						cur := pos
						mu.Unlock()
						if a.Value <= 0 {
							markDead("health")
						} else {
							markAlive(cur, "health")
						}
					}
				}
			case *packet.CommandOutput:
				for _, m := range p.OutputMessages {
					fmt.Printf("%s CMDOUT %s success=%t %s %v\n", ts(), name, m.Success, m.Message, m.Parameters)
				}
			case *packet.Text:
				if cmdLines != "" {
					fmt.Printf("%s TEXT %s %q\n", ts(), name, p.Message)
				}
			case *packet.ActorEvent:
				if p.EntityRuntimeID == gd.EntityRuntimeID && p.EventType == packet.ActorEventDeath {
					markDead("event")
				}
			case *packet.Respawn:
				if p.State == packet.RespawnStateReadyToSpawn {
					markAlive(p.Position, "respawn")
				}
			case *packet.ChangeDimension:
				mu.Lock()
				pos = p.Position
				mu.Unlock()
				botMu.Lock()
				st.lastPos = p.Position
				botMu.Unlock()
				fmt.Printf("%s DIM %s %d %.1f %.1f %.1f\n", ts(), name, p.Dimension, p.Position[0], p.Position[1], p.Position[2])
				lsid, dim := p.LoadingScreenID, p.Dimension
				_ = conn.WritePacket(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeStart, LoadingScreenID: lsid})
				_ = conn.Flush()
				time.AfterFunc(time.Second, func() {
					_ = conn.WritePacket(&packet.PlayerAction{EntityRuntimeID: gd.EntityRuntimeID, ActionType: protocol.PlayerActionDimensionChangeDone, BlockFace: -1})
					_ = conn.WritePacket(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeEnd, LoadingScreenID: lsid})
					_ = conn.Flush()
					teleported.Store(true)
					dimDone.Add(1)
					fmt.Printf("%s DIMDONE %s %d\n", ts(), name, dim)
				})
			case *packet.MovePlayer:
				if p.EntityRuntimeID == gd.EntityRuntimeID {
					mu.Lock()
					pos = p.Position
					mu.Unlock()
					botMu.Lock()
					st.lastPos = p.Position
					if p.Mode == packet.MoveModeTeleport {
						if st.teleports == 0 {
							tpBots.Add(1)
						}
						st.teleports++
					}
					botMu.Unlock()
					if p.Mode == packet.MoveModeTeleport {
						teleported.Store(true)
						fmt.Printf("%s TP %s %.1f %.1f %.1f\n", ts(), name, p.Position[0], p.Position[1], p.Position[2])
					} else if p.Mode == packet.MoveModeReset {
						teleported.Store(true)
					}
				}
			}
		}
	}()

	if radiusJitter > 0 {
		go func() {
			for {
				select {
				case <-stop:
					return
				case <-done:
					return
				case <-time.After(radiusJitter/2 + time.Duration(rand.Int63n(int64(radiusJitter)))):
					if !jitterOn.Load() {
						continue
					}
					r := int32(4 + rand.Intn(13))
					if err := conn.WritePacket(&packet.RequestChunkRadius{ChunkRadius: r, MaxChunkRadius: uint8(r)}); err != nil {
						return
					}
					_ = conn.Flush()
					txPackets.Add(1)
					fmt.Printf("%s RADIUS %s %d\n", ts(), name, r)
				}
			}
		}()
	}

	if probeEvery > 0 {
		go func() {
			pt := time.NewTicker(probeEvery)
			defer pt.Stop()
			for {
				select {
				case <-stop:
					return
				case <-done:
					return
				case <-pt.C:
					pendMu.Lock()
					if !pendAt.IsZero() {
						probeLost.Add(1)
					}
					pendTS = time.Now().UnixNano() / 1000
					pendAt = time.Now()
					pendMu.Unlock()
					var err error
					if probeKind == "nsl" {
						err = conn.WritePacket(&packet.NetworkStackLatency{Timestamp: pendTS, NeedsResponse: true})
					} else {
						err = conn.WritePacket(&packet.RequestChunkRadius{ChunkRadius: reqRadius, MaxChunkRadius: uint8(reqRadius)})
					}
					_ = conn.Flush()
					if err != nil {
						return
					}
					txPackets.Add(1)
				}
			}
		}()
	}

	ticker := time.NewTicker(time.Second / 20)
	defer ticker.Stop()
	const speed = 0.2158
	var tick uint64
	yaw := float64(len(name)*37%360) * math.Pi / 180
	for {
		select {
		case <-stop:
			return false, true
		case <-done:
			select {
			case <-stop:
				return false, true
			default:
			}
			return rejoinOnDrop, true
		case c := <-ch:
			if c != "reconnect" {
				fmt.Printf("%s CTL unknown command %s\n", ts(), c)
				continue
			}
			leaving.Store(true)
			_ = conn.Close()
			<-done
			left.Add(1)
			fmt.Printf("%s LEFT %s\n", ts(), name)
			return true, true
		case <-ticker.C:
			tick++
			yaw += 0.01
			dx := float32(-math.Sin(yaw) * speed)
			dz := float32(math.Cos(yaw) * speed)
			mu.Lock()
			pos = mgl32.Vec3{pos[0] + dx, pos[1], pos[2] + dz}
			cur := pos
			mu.Unlock()
			flags := protocol.NewInputFlags(packet.InputFlagCount)
			flags.Set(packet.InputFlagUp)
			flags.Set(packet.InputFlagVerticalCollision)
			if teleported.Swap(false) {
				flags.Set(packet.InputFlagHandledTeleport)
			}
			deg := float32(yaw * 180 / math.Pi)
			if err := conn.WritePacket(&packet.PlayerAuthInput{
				Position:           cur,
				MoveVector:         mgl32.Vec2{0, 1},
				AnalogueMoveVector: mgl32.Vec2{0, 1},
				RawMoveVector:      mgl32.Vec2{0, 1},
				HeadYaw:            deg,
				Yaw:                deg,
				InteractYaw:        deg,
				InputData:          flags,
				InputMode:          packet.InputModeMouse,
				PlayMode:           packet.PlayModeScreen,
				InteractionModel:   packet.InteractionModelCrosshair,
				Tick:               tick,
				Delta:              mgl32.Vec3{dx, 0, dz},
			}); err != nil {
				return false, true
			}
			txPackets.Add(1)
		}
	}
}

func column(lim int32) []protocol.SubChunkOffset {
	var out []protocol.SubChunkOffset
	hi := int8(19)
	if lim >= 0 && lim < 24 {
		hi = int8(lim) - 4
	}
	for y := int8(-4); y <= hi; y++ {
		out = append(out, protocol.SubChunkOffset{0, y, 0})
	}
	return out
}
