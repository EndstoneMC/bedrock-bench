// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/go-gl/mathgl/mgl32"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/login"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// syncBuffer collects the bot's stdout so the test can read it while the bot runs.
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

func (b *syncBuffer) events(t *testing.T) []map[string]any {
	var out []map[string]any
	sc := bufio.NewScanner(bytes.NewReader([]byte(b.String())))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var ev map[string]any
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			t.Fatalf("bad event line %q: %v", sc.Text(), err)
		}
		out = append(out, ev)
	}
	return out
}

func (b *syncBuffer) find(t *testing.T, name string) []map[string]any {
	var out []map[string]any
	for _, ev := range b.events(t) {
		if ev["ev"] == name {
			out = append(out, ev)
		}
	}
	return out
}

func waitUntil(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// fakeServer is a minimal in-process protocol 1001 server built on gophertunnel's listener. It records what the
// bot sends and echoes latency probes.
type fakeServer struct {
	l        *minecraft.Listener
	identity chan login.IdentityData
	conns    chan *minecraft.Conn

	mu       sync.Mutex
	received []packet.Packet
}

// startFakeServer listens on a free loopback port and serves one bot. cfg may set PacketFunc to see raw packets.
func startFakeServer(t *testing.T, cfg minecraft.ListenConfig) *fakeServer {
	t.Helper()
	cfg.AuthenticationDisabled = true
	cfg.StatusProvider = minecraft.NewStatusProvider("bedrock-bench", "loopback")
	l, err := cfg.Listen("raknet", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = l.Close() })
	s := &fakeServer{l: l, identity: make(chan login.IdentityData, 1), conns: make(chan *minecraft.Conn, 1)}
	go s.serve()
	return s
}

func (s *fakeServer) addr() string { return s.l.Addr().String() }

func (s *fakeServer) serve() {
	c, err := s.l.Accept()
	if err != nil {
		return
	}
	conn := c.(*minecraft.Conn)
	s.identity <- conn.IdentityData()
	gd := minecraft.GameData{
		WorldName:       "loopback",
		EntityUniqueID:  1,
		EntityRuntimeID: 1,
		PlayerPosition:  mgl32.Vec3{0.5, 70, 0.5},
		ChunkRadius:     4,
	}
	if err := conn.StartGame(gd); err != nil {
		return
	}
	s.conns <- conn
	for {
		pk, err := conn.ReadPacket()
		if err != nil {
			return
		}
		s.mu.Lock()
		s.received = append(s.received, pk)
		s.mu.Unlock()
		if p, ok := pk.(*packet.NetworkStackLatency); ok && p.NeedsResponse {
			_ = conn.WritePacket(&packet.NetworkStackLatency{Timestamp: p.Timestamp})
		}
	}
}

func (s *fakeServer) packets() []packet.Packet {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]packet.Packet(nil), s.received...)
}

// startBot runs the bot in the background and returns its stdout, stderr, stdin and exit code channel.
func startBot(t *testing.T, args ...string) (out, errOut *syncBuffer, stdin *io.PipeWriter, exit <-chan int) {
	t.Helper()
	stdinR, stdinW := io.Pipe()
	t.Cleanup(func() { _ = stdinW.Close() })
	out, errOut = &syncBuffer{}, &syncBuffer{}
	code := make(chan int, 1)
	go func() { code <- run(args, stdinR, out, errOut) }()
	return out, errOut, stdinW, code
}

// quitBot asks the bot to stop and checks that it exits cleanly.
func quitBot(t *testing.T, stdin io.Writer, exit <-chan int, errOut *syncBuffer) {
	t.Helper()
	_, _ = io.WriteString(stdin, "quit\n")
	select {
	case code := <-exit:
		if code != core.ExitOK {
			t.Errorf("exit %d; stderr:\n%s", code, errOut.String())
		}
	case <-time.After(20 * time.Second):
		t.Fatal("bot did not exit after quit")
	}
}

// TestLoopbackSession runs the real frontend against the fake server: login with a stable identity, spawn,
// teleport, chunks in both sub-chunk request modes and inline, every blob a miss, a movement correction, latency
// probes, a decode error and a kick.
func TestLoopbackSession(t *testing.T) {
	srv := startFakeServer(t, minecraft.ListenConfig{})
	out, errOut, stdin, exit := startBot(t,
		"-addr", srv.addr(), "-n", "1", "-prefix", "t", "-fake-xuid", "-radius", "8", "-probe-bots", "1",
		"-spawn-timeout", "20s", "-progress-interval", "50ms", "-stat-interval", "100ms", "-pkt-interval", "100ms",
	)

	var conn *minecraft.Conn
	select {
	case conn = <-srv.conns:
	case <-time.After(20 * time.Second):
		t.Fatalf("bot did not spawn; stderr:\n%s", errOut.String())
	}
	id := <-srv.identity
	if id.DisplayName != "t00" || id.Identity != core.BotUUID("bedrock-bench", "t00") {
		t.Errorf("identity = %+v", id)
	}
	if id.XUID != "2535400000000000" {
		t.Errorf("XUID = %q, want the fake XUID", id.XUID)
	}

	waitUntil(t, "walking input", func() bool {
		for _, pk := range srv.packets() {
			if _, ok := pk.(*packet.PlayerAuthInput); ok {
				return true
			}
		}
		return false
	})
	_ = conn.WritePacket(&packet.MovePlayer{
		EntityRuntimeID: 1, Position: mgl32.Vec3{1280.5, 301.62, 1280.5}, Mode: packet.MoveModeTeleport,
	})
	for x := int32(79); x <= 81; x++ {
		for z := int32(79); z <= 81; z++ {
			_ = conn.WritePacket(&packet.LevelChunk{
				Position: protocol.ChunkPos{x, z}, SubChunkCount: protocol.SubChunkRequestModeLimitless,
				CacheEnabled: true, BlobHashes: []uint64{uint64(x)<<32 | uint64(z)}, RawPayload: []byte{0},
			})
		}
	}
	_ = conn.WritePacket(&packet.LevelChunk{
		Position: protocol.ChunkPos{82, 80}, SubChunkCount: protocol.SubChunkRequestModeLimited, HighestSubChunk: 10,
		RawPayload: []byte{0},
	})
	// PocketMine-MP sends its sub-chunks inside the chunk packet; nothing is requested for it.
	inline := []byte{0}
	_ = conn.WritePacket(&packet.LevelChunk{Position: protocol.ChunkPos{80, 82}, SubChunkCount: 4, RawPayload: inline})
	// A resend of a chunk the bot already has.
	_ = conn.WritePacket(&packet.LevelChunk{Position: protocol.ChunkPos{79, 79}, SubChunkCount: 4, RawPayload: inline})
	_ = conn.WritePacket(&packet.SubChunk{
		CacheEnabled: true, Position: protocol.SubChunkPos{80, 0, 80},
		SubChunkEntries: []protocol.SubChunkEntry{
			{Offset: protocol.SubChunkOffset{0, -4, 0}, Result: protocol.SubChunkResultSuccess, RawPayload: inline, BlobHash: 7},
			{Offset: protocol.SubChunkOffset{0, -3, 0}, Result: protocol.SubChunkResultSuccessAllAir, BlobHash: 8},
		},
	})
	_ = conn.WritePacket(&packet.CorrectPlayerMovePrediction{Position: mgl32.Vec3{1280.5, 300, 1281}})
	// A SubChunk packet cut short after its first field: the bot cannot decode it.
	_, _ = conn.Write(append(binary.AppendUvarint(nil, packet.IDSubChunk), 0x01))
	_ = conn.Flush()

	waitUntil(t, "a latency probe round trip", func() bool {
		for _, ev := range out.find(t, core.EvRTT) {
			if ev["n"].(float64) >= 1 {
				return true
			}
		}
		return false
	})
	waitUntil(t, "chunk accounting", func() bool {
		for _, ev := range out.find(t, core.EvStat) {
			if ev["uniq"] == 11.0 && ev["corrections"] == 1.0 && ev["decode_errors"] == 1.0 {
				return true
			}
		}
		return false
	})
	_ = srv.l.Disconnect(conn, "bench over")
	waitUntil(t, "the kick", func() bool { return len(out.find(t, core.EvKicked)) == 1 })
	quitBot(t, stdin, exit, errOut)

	checkBotEvents(t, out)
	checkServerSaw(t, srv.packets())
}

func checkBotEvents(t *testing.T, out *syncBuffer) {
	t.Helper()
	spawned := out.find(t, core.EvSpawned)
	if len(spawned) != 1 || spawned[0]["radius"] != 4.0 || spawned[0]["session"] != 1.0 {
		t.Errorf("spawned = %v", spawned)
	}
	tp := out.find(t, core.EvTP)
	if len(tp) != 1 {
		t.Fatalf("tp = %v", tp)
	}
	if pos := tp[0]["pos"].([]any); pos[0] != 1280.5 || pos[1] != 301.62 || pos[2] != 1280.5 {
		t.Errorf("tp pos = %v", pos)
	}
	if fc := out.find(t, core.EvFirstChunk); len(fc) != 1 {
		t.Errorf("first_chunk = %v", fc)
	}
	de := out.find(t, core.EvDecodeError)
	if len(de) != 1 || de[0]["pkt_id"] != float64(packet.IDSubChunk) || de[0]["bot"] != "t00" {
		t.Errorf("decode_error = %v", de)
	}
	kicked := out.find(t, core.EvKicked)
	if len(kicked) != 1 || kicked[0]["reason"] != "bench over" {
		t.Errorf("kicked = %v", kicked)
	}
	stats := out.find(t, core.EvStat)
	last := stats[len(stats)-1]
	if last["level_chunks"] != 12.0 || last["subchunks"] != 2.0 || last["dropped"] != 1.0 || last["kicked"] != 1.0 ||
		last["decode_errors"] != 1.0 {
		t.Errorf("final stat = %v", last)
	}
	if last["rx_bytes"].(float64) <= 0 || last["tx_bytes"].(float64) <= 0 {
		t.Errorf("byte counters not advancing: %v", last)
	}
	summary := out.find(t, core.EvBotSummary)
	if len(summary) != 1 {
		t.Fatalf("bot_summary = %v", summary)
	}
	s := summary[0]
	if s["uniq"] != 11.0 || s["in_view"] != 11.0 || s["chunks"] != 12.0 || s["corrections"] != 1.0 {
		t.Errorf("bot_summary = %v", s)
	}
	if fs := s["first_seen_ms"].([]any); len(fs) != 11 {
		t.Errorf("first_seen_ms = %v", fs)
	}
	var levelChunks float64
	for _, ev := range out.find(t, core.EvPkt) {
		if n, ok := ev["rx"].(map[string]any)["LevelChunk"].(float64); ok {
			levelChunks += n
		}
	}
	if levelChunks != 12 {
		t.Errorf("pkt events count %v LevelChunk packets, want 12", levelChunks)
	}
}

func checkServerSaw(t *testing.T, pks []packet.Packet) {
	t.Helper()
	var (
		radius8, screenStart, screenEnd, blobMisses, probes int
		offsets                                             []int
		handledTP                                           bool
		lastPos                                             mgl32.Vec3
	)
	for _, pk := range pks {
		switch p := pk.(type) {
		case *packet.RequestChunkRadius:
			if p.ChunkRadius == 8 {
				radius8++
			}
		case *packet.ServerBoundLoadingScreen:
			switch p.Type {
			case packet.LoadingScreenTypeStart:
				screenStart++
			case packet.LoadingScreenTypeEnd:
				screenEnd++
			}
		case *packet.ClientCacheBlobStatus:
			blobMisses += len(p.MissHashes)
		case *packet.SubChunkRequest:
			offsets = append(offsets, len(p.Offsets))
			if p.Offsets[0][1] != -4 {
				t.Errorf("sub-chunk request starts at y=%d, want -4", p.Offsets[0][1])
			}
		case *packet.NetworkStackLatency:
			if p.NeedsResponse {
				probes++
			}
		case *packet.PlayerAuthInput:
			if p.InputData.Load(packet.InputFlagHandledTeleport) {
				handledTP = true
			}
			lastPos = p.Position
		}
	}
	if radius8 != 1 || screenStart != 1 || screenEnd != 1 {
		t.Errorf("radius requests %d, loading screen start %d end %d; want 1 each", radius8, screenStart, screenEnd)
	}
	if blobMisses != 11 {
		t.Errorf("%d blob misses, want 9 from chunks and 2 from sub-chunks", blobMisses)
	}
	if fmt.Sprint(offsets) != fmt.Sprint([]int{24, 24, 24, 24, 24, 24, 24, 24, 24, 10}) {
		t.Errorf("sub-chunk request sizes = %v", offsets)
	}
	if probes == 0 {
		t.Error("no latency probes sent")
	}
	if !handledTP {
		t.Error("teleport never acknowledged in PlayerAuthInput")
	}
	if d := math.Hypot(float64(lastPos[0]-1280.5), float64(lastPos[2]-1281)); d > 2*core.WalkSpeed/core.TurnRate+1 {
		t.Errorf("last input position %v is not on a circle near the teleport target", lastPos)
	}
}
