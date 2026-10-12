// Command loadbot joins N offline-mode bots to a BDS server and keeps them walking.
package main

import (
	"context"
	"flag"
	"fmt"
	"math"
	"os"
	"os/signal"
	"sort"
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

	probeEvery time.Duration
	probeKind  string
	rttMu      sync.Mutex
	rtts       []time.Duration
	probeLost  atomic.Int64
)

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
	flag.Parse()

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
loop:
	for time.Since(t0) < *dur {
		select {
		case <-ctx.Done():
			break loop
		case <-ticker.C:
			rx, tx := rxPackets.Load(), txPackets.Load()
			fmt.Printf("%s STAT online=%d dropped=%d rx/s=%.0f tx/s=%.0f corrections=%d chunks=%d\n", ts(),
				joined.Load()-dropped.Load(), dropped.Load(), float64(rx-lastRx)/10, float64(tx-lastTx)/10, corrections.Load(), chunks.Load())
			lastRx, lastTx = rx, tx
		case <-time.After(time.Until(t0.Add(*dur))):
			break loop
		}
	}
	fmt.Printf("%s %s\n", ts(), rttSummary())
	fmt.Printf("%s DONE joined=%d failed=%d dropped=%d rx=%d tx=%d corrections=%d chunks=%d\n", ts(),
		joined.Load(), failed.Load(), dropped.Load(), rxPackets.Load(), txPackets.Load(), corrections.Load(), chunks.Load())
	close(stop)
	wg.Wait()
}

func ts() string { return time.Now().Format("15:04:05.000") }

func runBot(ctx context.Context, addr, name string, ready chan<- struct{}, stop <-chan struct{}) {
	dialer := minecraft.Dialer{
		IdentityData: login.IdentityData{DisplayName: name, Identity: uuid.NewString()},
		EnableClientCache: true,
		DownloadResourcePack: func(uuid.UUID, string, int, int) bool { return false },
	}
	dctx, dcancel := context.WithTimeout(ctx, 60*time.Second)
	defer dcancel()
	conn, err := dialer.DialContext(dctx, "raknet", addr)
	if err != nil {
		failed.Add(1)
		fmt.Printf("%s %s DIAL FAILED: %v\n", ts(), name, err)
		return
	}
	defer conn.Close()
	if err := conn.DoSpawnContext(dctx); err != nil {
		failed.Add(1)
		fmt.Printf("%s %s SPAWN FAILED: %v\n", ts(), name, err)
		return
	}
	joined.Add(1)
	ready <- struct{}{}

	gd := conn.GameData()
	var mu sync.Mutex
	pos := gd.PlayerPosition
	var pendMu sync.Mutex
	var pendAt time.Time
	var pendTS int64
	var teleported atomic.Bool
	done := make(chan struct{})

	go func() {
		defer close(done)
		for {
			pk, err := conn.ReadPacket()
			if err != nil {
				select {
				case <-stop:
				default:
					dropped.Add(1)
					fmt.Printf("%s %s READ FAILED: %v\n", ts(), name, err)
				}
				return
			}
			rxPackets.Add(1)
			switch p := pk.(type) {
			case *packet.LevelChunk:
				chunks.Add(1)
				if len(p.BlobHashes) > 0 {
					_ = conn.WritePacket(&packet.ClientCacheBlobStatus{MissHashes: p.BlobHashes})
					txPackets.Add(1)
				}
				if _, ok := p.SubChunkLimit.Value(); ok {
					_ = conn.WritePacket(&packet.SubChunkRequest{
						Dimension: p.Dimension,
						Offsets:   column(),
						Position:  protocol.SubChunkPos{p.Position[0], 0, p.Position[1]},
					})
					txPackets.Add(1)
				}
			case *packet.SubChunk:
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
			case *packet.MovePlayer:
				if p.EntityRuntimeID == gd.EntityRuntimeID {
					mu.Lock()
					pos = p.Position
					mu.Unlock()
					if p.Mode == packet.MoveModeTeleport {
						teleported.Store(true)
					}
				}
			}
		}
	}()

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
						err = conn.WritePacket(&packet.RequestChunkRadius{ChunkRadius: 16, MaxChunkRadius: 16})
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
	tick := uint64(gd.Time)
	yaw := float64(len(name)*37%360) * math.Pi / 180
	for {
		select {
		case <-stop:
			return
		case <-done:
			return
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
				return
			}
			txPackets.Add(1)
		}
	}
}

func column() []protocol.SubChunkOffset {
	var out []protocol.SubChunkOffset
	for y := int8(-4); y <= 5; y++ {
		out = append(out, protocol.SubChunkOffset{0, y, 0})
	}
	return out
}
