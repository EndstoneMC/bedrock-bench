// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"strings"
	"sync"
	"testing"
	"time"
)

func init() {
	RetryDelay = 10 * time.Millisecond
	ShutdownGrace = 2 * time.Second
}

// syncBuffer collects emitted lines and lets a test wait for one.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) events(t *testing.T) []map[string]any {
	b.mu.Lock()
	data := append([]byte(nil), b.buf.Bytes()...)
	b.mu.Unlock()
	var out []map[string]any
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var ev map[string]any
		if err := json.Unmarshal(sc.Bytes(), &ev); err != nil {
			t.Fatalf("bad line %q: %v", sc.Text(), err)
		}
		out = append(out, ev)
	}
	return out
}

func (b *syncBuffer) waitFor(t *testing.T, match func(map[string]any) bool) map[string]any {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		for _, ev := range b.events(t) {
			if match(ev) {
				return ev
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("event not seen; output:\n%s", b.buf.String())
	return nil
}

func isEv(name string) func(map[string]any) bool {
	return func(ev map[string]any) bool { return ev["ev"] == name }
}

func filter(evs []map[string]any, name string) []map[string]any {
	var out []map[string]any
	for _, ev := range evs {
		if ev["ev"] == name {
			out = append(out, ev)
		}
	}
	return out
}

// fakeFrontend spawns every bot at a fixed spot, teleports it to the grid and delivers a 3x3 patch of chunks.
type fakeFrontend struct {
	mu       sync.Mutex
	failures map[int]int // bot index -> failing attempts before success (-1: always)
	attempts map[int]int
	onSpawn  func(s *Session)
}

func (f *fakeFrontend) Build() BuildInfo {
	return BuildInfo{Module: "loadbot", Version: "test", Protocol: 2193}
}

func (f *fakeFrontend) PacketNames() map[uint32]string { return map[uint32]string{58: "LevelChunk"} }

func (f *fakeFrontend) RunSession(ctx context.Context, s *Session) SessionEnd {
	f.mu.Lock()
	if f.attempts == nil {
		f.attempts = map[int]int{}
	}
	f.attempts[s.Bot.Index]++
	attempt, fails := f.attempts[s.Bot.Index], f.failures[s.Bot.Index]
	f.mu.Unlock()
	if fails < 0 || attempt <= fails {
		return SessionEnd{Reason: EndJoinFailed, Stage: StageSpawn, Err: errors.New("context deadline exceeded")}
	}
	s.Spawned(0.5, 70, 0.5, DimOverworld, 2)
	s.LevelChunk(DimOverworld, 0, 0) // spawn area, before the teleport: not counted
	x := 1280.5 + float64(s.Bot.Index)*640
	s.Teleported(x, 301.62, 1280.5)
	cx := BlockToChunk(x)
	for dx := int32(-1); dx <= 1; dx++ {
		for dz := int32(-1); dz <= 1; dz++ {
			s.LevelChunk(DimOverworld, cx+dx, 80+dz)
			s.RxPacket(58)
		}
	}
	s.Corrected(x, 300, 1280.5)
	if f.onSpawn != nil {
		f.onSpawn(s)
	}
	select {
	case <-ctx.Done():
		return SessionEnd{Reason: EndStopped}
	case <-s.Leave():
		return SessionEnd{Reason: EndLeft}
	}
}

func testConfig(t *testing.T, args ...string) *Config {
	t.Helper()
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	var c Config
	c.Register(fs)
	base := []string{"-addr", "127.0.0.1:1", "-prefix", "a", "-stagger", "0s",
		"-progress-interval", "20ms", "-stat-interval", "20ms", "-pkt-interval", "20ms"}
	if err := fs.Parse(append(base, args...)); err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatal(err)
	}
	return &c
}

type fleetRun struct {
	out   *syncBuffer
	stdin *io.PipeWriter
	exit  chan int
	errw  *syncBuffer
}

func startFleet(t *testing.T, cfg *Config, fe Frontend) *fleetRun {
	t.Helper()
	r := &fleetRun{out: &syncBuffer{}, errw: &syncBuffer{}, exit: make(chan int, 1)}
	pr, pw := io.Pipe()
	r.stdin = pw
	f := NewFleet(cfg, fe, NewEmitter(r.out, NewSystemClock()), r.errw)
	go func() { r.exit <- f.Run(context.Background(), pr, map[string]any{"n": cfg.N}) }()
	t.Cleanup(func() { _ = pw.Close() })
	return r
}

func (r *fleetRun) send(t *testing.T, line string) {
	t.Helper()
	if _, err := io.WriteString(r.stdin, line+"\n"); err != nil {
		t.Fatal(err)
	}
}

func (r *fleetRun) wait(t *testing.T) int {
	t.Helper()
	select {
	case code := <-r.exit:
		return code
	case <-time.After(10 * time.Second):
		t.Fatalf("fleet did not exit; output:\n%s", r.out.buf.String())
		return -1
	}
}

func TestFleetJoinsReportsAndQuits(t *testing.T) {
	cfg := testConfig(t, "-n", "3")
	r := startFleet(t, cfg, &fakeFrontend{failures: map[int]int{1: -1}})
	all := r.out.waitFor(t, isEv(EvAllJoined))
	if all["joined"] != 2.0 || all["failed"] != 1.0 {
		t.Errorf("alljoined = %v", all)
	}
	r.out.waitFor(t, func(ev map[string]any) bool { return ev["ev"] == EvStat && ev["uniq"] == 18.0 })
	r.send(t, "quit")
	if code := r.wait(t); code != ExitOK {
		t.Errorf("exit %d, want 0", code)
	}

	evs := r.out.events(t)
	if evs[0]["ev"] != EvHello || evs[len(evs)-1]["ev"] != EvDone {
		t.Errorf("first %v, last %v", evs[0]["ev"], evs[len(evs)-1]["ev"])
	}
	failed := filter(evs, EvJoinFailed)
	if len(failed) != 1 || failed[0]["bot"] != "a01" || failed[0]["stage"] != StageSpawn || failed[0]["final"] != true {
		t.Errorf("join_failed = %v", failed)
	}
	if n := len(filter(evs, EvTP)); n != 2 {
		t.Errorf("%d tp events, want 2", n)
	}
	if n := len(filter(evs, EvFirstChunk)); n != 2 {
		t.Errorf("%d first_chunk events, want 2", n)
	}
	summaries := filter(evs, EvBotSummary)
	if len(summaries) != 2 {
		t.Fatalf("bot_summary = %v", summaries)
	}
	for _, s := range summaries {
		if s["uniq"] != 9.0 || s["in_view"] != 9.0 || s["chunks"] != 9.0 || s["corrections"] != 1.0 {
			t.Errorf("summary = %v", s)
		}
		if fs := s["first_seen_ms"].([]any); len(fs) != 9 {
			t.Errorf("first_seen_ms = %v", fs)
		}
	}
	var progress map[string]any
	for _, ev := range filter(evs, EvProgress) {
		progress = ev
	}
	rows := progress["rows"].([]any)
	if len(rows) != 3 {
		t.Fatalf("rows = %v", rows)
	}
	failedRow := rows[1].([]any)
	if failedRow[0] != "a01" || failedRow[3] != nil || failedRow[6] != nil {
		t.Errorf("row of a bot that never spawned = %v", failedRow)
	}
	placed := rows[2].([]any)
	if placed[0] != "a02" || placed[3] != 2560.5 || placed[4] != 300.0 || placed[7] != 1.0 || placed[8] != 1.0 {
		t.Errorf("row = %v", placed)
	}
	stats := filter(evs, EvStat)
	last := stats[len(stats)-1]
	if last["level_chunks"] != 20.0 || last["corrections"] != 2.0 || last["failed"] != 1.0 || last["online"] != 2.0 {
		t.Errorf("final stat = %v", last)
	}
}

func TestFleetAllJoinsFailedExits2(t *testing.T) {
	cfg := testConfig(t, "-n", "2")
	r := startFleet(t, cfg, &fakeFrontend{failures: map[int]int{0: -1, 1: -1}})
	if code := r.wait(t); code != ExitAllFailed {
		t.Errorf("exit %d, want %d", code, ExitAllFailed)
	}
	evs := r.out.events(t)
	if len(filter(evs, EvBotSummary)) != 0 {
		t.Error("bots that never spawned must not get a summary")
	}
	if evs[len(evs)-1]["ev"] != EvDone {
		t.Error("done must be last")
	}
}

func TestFleetJoinRetries(t *testing.T) {
	cfg := testConfig(t, "-n", "1", "-join-retries", "2")
	r := startFleet(t, cfg, &fakeFrontend{failures: map[int]int{0: 2}})
	r.out.waitFor(t, isEv(EvAllJoined))
	r.send(t, "quit")
	if code := r.wait(t); code != ExitOK {
		t.Errorf("exit %d", code)
	}
	evs := r.out.events(t)
	failed := filter(evs, EvJoinFailed)
	if len(failed) != 2 || failed[0]["final"] != false || failed[1]["attempt"] != 2.0 {
		t.Errorf("join_failed = %v", failed)
	}
	if n := len(filter(evs, EvDial)); n != 3 {
		t.Errorf("%d dial events, want one per attempt (3)", n)
	}
	sp := filter(evs, EvSpawned)
	if len(sp) != 1 || sp[0]["session"] != 1.0 {
		t.Errorf("spawned = %v", sp)
	}
}

func TestFleetDecodeErrorFailExits3(t *testing.T) {
	cfg := testConfig(t, "-n", "1", "-decode-errors", "fail")
	fe := &fakeFrontend{onSpawn: func(s *Session) {
		id := uint32(174)
		s.DecodeError(&id, "decode packet *packet.SubChunk: unexpected EOF")
	}}
	r := startFleet(t, cfg, fe)
	if code := r.wait(t); code != ExitDecodeError {
		t.Errorf("exit %d, want %d", code, ExitDecodeError)
	}
	evs := r.out.events(t)
	de := filter(evs, EvDecodeError)
	if len(de) != 1 || de[0]["pkt_id"] != 174.0 || de[0]["total"] != 1.0 {
		t.Errorf("decode_error = %v", de)
	}
	stats := filter(evs, EvStat)
	if stats[len(stats)-1]["decode_errors"] != 1.0 {
		t.Errorf("stat = %v", stats[len(stats)-1])
	}
}

func TestFleetReconnectAndDisconnect(t *testing.T) {
	cfg := testConfig(t, "-n", "2", "-control")
	r := startFleet(t, cfg, &fakeFrontend{})
	r.out.waitFor(t, isEv(EvAllJoined))

	r.send(t, `{"op":"reconnect","bot":"a00","delay_ms":0}`)
	r.out.waitFor(t, func(ev map[string]any) bool { return ev["ev"] == EvSpawned && ev["session"] == 2.0 })

	r.send(t, `{"op":"disconnect","i":1}`)
	r.out.waitFor(t, func(ev map[string]any) bool { return ev["ev"] == EvLeft && ev["bot"] == "a01" })
	r.send(t, `{"op":"reconnect","i":1,"delay_ms":0}`)
	r.out.waitFor(t, func(ev map[string]any) bool {
		return ev["ev"] == EvSpawned && ev["bot"] == "a01" && ev["session"] == 2.0
	})
	r.out.waitFor(t, func(ev map[string]any) bool {
		return ev["ev"] == EvStat && ev["rejoins"] == 2.0 && ev["left"] == 2.0 && ev["online"] == 2.0
	})
	r.send(t, `{"op":"stop"}`)
	if code := r.wait(t); code != ExitOK {
		t.Errorf("exit %d", code)
	}
	evs := r.out.events(t)
	if n := len(filter(evs, EvRejoin)); n != 2 {
		t.Errorf("%d rejoin events, want 2", n)
	}
	if n := len(filter(evs, EvDropped)) + len(filter(evs, EvKicked)); n != 0 {
		t.Errorf("control-initiated leaves must not count as drops, got %d", n)
	}
}

func TestFleetIgnoresControlWithoutFlag(t *testing.T) {
	cfg := testConfig(t, "-n", "1")
	r := startFleet(t, cfg, &fakeFrontend{})
	r.out.waitFor(t, isEv(EvAllJoined))
	r.send(t, `{"op":"disconnect","i":0}`)
	r.send(t, "quit")
	r.wait(t)
	if n := len(filter(r.out.events(t), EvLeft)); n != 0 {
		t.Errorf("JSON op applied without -control")
	}
	if !strings.Contains(r.errw.buf.String(), "only \"quit\"") {
		t.Errorf("stderr = %q", r.errw.buf.String())
	}
}

func TestFleetStopsAtEndOfStdin(t *testing.T) {
	cfg := testConfig(t, "-n", "1")
	r := startFleet(t, cfg, &fakeFrontend{})
	r.out.waitFor(t, isEv(EvAllJoined))
	_ = r.stdin.Close()
	if code := r.wait(t); code != ExitOK {
		t.Errorf("exit %d", code)
	}
}

func TestFleetStopsOnContext(t *testing.T) {
	cfg := testConfig(t, "-n", "1")
	out := &syncBuffer{}
	ctx, cancel := context.WithCancel(context.Background())
	pr, pw := io.Pipe()
	defer pw.Close()
	f := NewFleet(cfg, &fakeFrontend{}, NewEmitter(out, NewSystemClock()), io.Discard)
	done := make(chan int, 1)
	go func() { done <- f.Run(ctx, pr, nil) }()
	out.waitFor(t, isEv(EvAllJoined))
	cancel()
	select {
	case code := <-done:
		if code != ExitOK {
			t.Errorf("exit %d", code)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("fleet ignored the signal context")
	}
}
