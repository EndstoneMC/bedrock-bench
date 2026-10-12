// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"math"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestBotUUIDMatchesRFC9562(t *testing.T) {
	// Reference values from Python: uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, seed), name).
	cases := []struct{ seed, name, want string }{
		{"bedrock-bench", "a00", "916f55f9-f355-5753-a1a2-e86b3ff0ead4"},
		{"bedrock-bench", "a07", "f772d804-dfc0-5864-aca1-62c2503e67b5"},
		{"bedrock-bench", "b00", "cfa2ea17-c22d-59c6-8d04-476ee9e828bd"},
		{"12345678-1234-5678-1234-567812345678", "a00", "ca856aba-4cc7-5c1d-904f-e10b60f2c988"},
	}
	for _, c := range cases {
		if got := BotUUID(c.seed, c.name); got != c.want {
			t.Errorf("BotUUID(%q, %q) = %s, want %s", c.seed, c.name, got, c.want)
		}
	}
	if got := Namespace("bedrock-bench").String(); got != "a63ab145-9c09-5f88-b7a5-cbc4f1aa9bb9" {
		t.Errorf("Namespace = %s", got)
	}
}

func TestParseUUIDRejectsMalformed(t *testing.T) {
	for _, s := range []string{"", "bedrock-bench", "12345678-1234-5678-1234-56781234567g", "123456781234567812345678123456781234"} {
		if _, err := ParseUUID(s); err == nil {
			t.Errorf("ParseUUID(%q) accepted", s)
		}
	}
}

func TestFakeXUID(t *testing.T) {
	if got := FakeXUID(7); got != "2535400000000007" {
		t.Errorf("FakeXUID(7) = %s", got)
	}
}

func TestStartYawGoldenAngle(t *testing.T) {
	if StartYaw(0) != 0 {
		t.Errorf("StartYaw(0) = %v", StartYaw(0))
	}
	got := StartYaw(1) * 180 / math.Pi
	if math.Abs(got-137.508) > 1e-9 {
		t.Errorf("StartYaw(1) = %v°", got)
	}
	got = StartYaw(3) * 180 / math.Pi
	if math.Abs(got-math.Mod(3*137.508, 360)) > 1e-9 {
		t.Errorf("StartYaw(3) = %v°", got)
	}
	seen := map[int]bool{}
	for i := 0; i < 100; i++ {
		seen[int(StartYaw(i)*180/math.Pi)] = true
	}
	if len(seen) < 95 {
		t.Errorf("100 bots share start yaws: only %d distinct whole degrees", len(seen))
	}
}

func TestCircleWalkPeriodAndRadius(t *testing.T) {
	w := NewWalker(WalkCircle, 5)
	var x, z, maxDist float64
	for tick := 1; tick <= 628; tick++ {
		dx, dz := w.Step()
		if step := math.Hypot(dx, dz); math.Abs(step-WalkSpeed) > 1e-9 {
			t.Fatalf("tick %d: step %v, want %v", tick, step, WalkSpeed)
		}
		x, z = x+dx, z+dz
		maxDist = math.Max(maxDist, math.Hypot(x, z))
	}
	if d := math.Hypot(x, z); d > 0.1 {
		t.Errorf("after 628 ticks the bot is %.3f blocks from its start, want a closed circle", d)
	}
	diameter := 2 * WalkSpeed / TurnRate
	if math.Abs(maxDist-diameter) > 0.1 {
		t.Errorf("widest distance %.2f, want the diameter %.2f", maxDist, diameter)
	}
	if y := w.YawDegrees(); y < -180 || y >= 180 {
		t.Errorf("yaw %v outside [-180, 180)", y)
	}
}

func TestWalkNoneStandsStill(t *testing.T) {
	w := NewWalker(WalkNone, 3)
	for i := 0; i < 10; i++ {
		if dx, dz := w.Step(); dx != 0 || dz != 0 {
			t.Fatalf("walk none moved by %v,%v", dx, dz)
		}
	}
}

func TestSubChunkYOffsets(t *testing.T) {
	cases := []struct {
		dim   int32
		limit int32
		first int8
		n     int
	}{
		{DimOverworld, -1, -4, 24},
		{DimOverworld, 24, -4, 24},
		{DimOverworld, 30, -4, 24},
		{DimOverworld, 10, -4, 10},
		{DimOverworld, 0, -4, 0},
		{DimNether, -1, 0, 8},
		{DimNether, 5, 0, 5},
		{DimEnd, -1, 0, 16},
		{DimEnd, 16, 0, 16},
	}
	for _, c := range cases {
		got := SubChunkYOffsets(c.dim, c.limit)
		if len(got) != c.n {
			t.Errorf("dim %d limit %d: %d offsets, want %d", c.dim, c.limit, len(got), c.n)
			continue
		}
		for i, y := range got {
			if y != c.first+int8(i) {
				t.Errorf("dim %d limit %d: offset %d is %d, want %d", c.dim, c.limit, i, y, c.first+int8(i))
			}
		}
	}
}

func TestChunkTrackerInViewByDimension(t *testing.T) {
	tr := NewChunkTracker()
	if tr.Add(ChunkKey{0, 0, 0}, 5) {
		t.Fatal("chunks before the first teleport must not count")
	}
	tr.Start(1000)
	// A disc of radius 2 around (80, 80) in the overworld, the same columns in the nether, and an old view.
	for x := int32(78); x <= 82; x++ {
		for z := int32(78); z <= 82; z++ {
			tr.Add(ChunkKey{0, x, z}, 1100)
			tr.Add(ChunkKey{1, x, z}, 1100)
		}
	}
	tr.Add(ChunkKey{0, 0, 0}, 1200)
	tr.Add(ChunkKey{0, 80, 80}, 1300) // resend
	if got, want := tr.Uniq(), 25*2+1; got != want {
		t.Errorf("Uniq = %d, want %d", got, want)
	}
	if got := tr.Received(); got != 52 {
		t.Errorf("Received = %d, want 52", got)
	}
	// Radius 1 counts within 2 chunks Euclidean: 13 columns of the 5x5 square.
	if got := tr.InView(0, 80, 80, 1); got != 13 {
		t.Errorf("InView overworld r=1 = %d, want 13", got)
	}
	if got := tr.InView(1, 80, 80, 1); got != 13 {
		t.Errorf("InView nether r=1 = %d, want 13", got)
	}
	if got := tr.InView(2, 80, 80, 10); got != 0 {
		t.Errorf("InView end = %d, want 0", got)
	}
	if got := tr.InView(0, 0, 0, 1); got != 1 {
		t.Errorf("InView at the old view = %d, want 1", got)
	}
}

func TestDiscOfRadiusTen(t *testing.T) {
	n := 0
	for x := int32(-12); x <= 12; x++ {
		for z := int32(-12); z <= 12; z++ {
			if InView(ChunkKey{0, x, z}, 0, 0, 0, 9) {
				n++
			}
		}
	}
	if n != 317 {
		t.Errorf("disc(10) has %d columns, want 317", n)
	}
}

func TestFirstSeenDeltas(t *testing.T) {
	tr := NewChunkTracker()
	tr.Start(10_000)
	tr.Add(ChunkKey{0, 0, 0}, 11_430.7)
	tr.Add(ChunkKey{0, 50, 50}, 11_500) // out of view, skipped
	tr.Add(ChunkKey{0, 1, 0}, 11_430.9)
	tr.Add(ChunkKey{0, 0, 1}, 11_442)
	tr.Add(ChunkKey{0, 1, 1}, 11_445)
	got := tr.FirstSeenDeltas(0, 0, 0, 4)
	want := []int64{1430, 0, 12, 3}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("FirstSeenDeltas = %v, want %v", got, want)
	}
	if got := NewChunkTracker().FirstSeenDeltas(0, 0, 0, 4); got == nil || len(got) != 0 {
		t.Errorf("empty tracker gives %#v, want an empty non-nil slice", got)
	}
}

func TestBlockToChunkFloors(t *testing.T) {
	for _, c := range []struct {
		v    float64
		want int32
	}{{0, 0}, {15.9, 0}, {16, 1}, {-0.1, -1}, {-16, -1}, {-16.1, -2}, {1280.5, 80}} {
		if got := BlockToChunk(c.v); got != c.want {
			t.Errorf("BlockToChunk(%v) = %d, want %d", c.v, got, c.want)
		}
	}
}

func TestParseCommand(t *testing.T) {
	five := 5
	good := []struct {
		line    string
		control bool
		want    Command
	}{
		{"quit", false, Command{Op: OpQuit}},
		{"  quit\r", true, Command{Op: OpQuit}},
		{`{"op":"stop"}`, true, Command{Op: OpQuit}},
		{`{"op":"reconnect","bot":"a07"}`, true, Command{Op: OpReconnect, Bot: "a07"}},
		{`{"op":"reconnect","i":5,"delay_ms":5}`, true, Command{Op: OpReconnect, I: &five, DelayMS: &five}},
		{`{"op":"disconnect","i":5}`, true, Command{Op: OpDisconnect, I: &five}},
		{`{"op":"radius","radius":8}`, true, Command{Op: OpRadius, Radius: 8}},
		{`{"op":"radius","radius":8,"bot":"a01"}`, true, Command{Op: OpRadius, Radius: 8, Bot: "a01"}},
	}
	for _, c := range good {
		got, err := ParseCommand(c.line, c.control)
		if err != nil {
			t.Errorf("ParseCommand(%q): %v", c.line, err)
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("ParseCommand(%q) = %+v, want %+v", c.line, got, c.want)
		}
	}
	bad := []struct {
		line    string
		control bool
	}{
		{`{"op":"reconnect","bot":"a07"}`, false},
		{"reconnect a07", true},
		{`{"op":"reconnect"}`, true},
		{`{"op":"reconnect","i":-1}`, true},
		{`{"op":"disconnect","bot":"a1","delay_ms":10}`, true},
		{`{"op":"jitter","on":true}`, true},
		{`{"op":"radius","radius":0}`, true},
		{`{"op":"radius","radius":97}`, true},
		{`{"op":"radius","r":8}`, true},
		{`{"op":"stop","bot":"a1"}`, true},
		{`{"op":"dance"}`, true},
		{`{"op":"radius","radius":8,"extra":1}`, true},
		{`{"op":"radius","radius":8} {}`, true},
	}
	for _, c := range bad {
		if got, err := ParseCommand(c.line, c.control); err == nil {
			t.Errorf("ParseCommand(%q, control=%v) = %+v, want an error", c.line, c.control, got)
		}
	}
	if _, err := ParseCommand("   ", true); !errors.Is(err, ErrEmptyLine) {
		t.Errorf("blank line: %v", err)
	}
}

func TestPercentileNearestRank(t *testing.T) {
	v := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
	if got := Percentile(v, 0.5); got != 6 { // round(0.5*9) = round(4.5) = 5 -> sorted[5]
		t.Errorf("p50 = %v, want 6", got)
	}
	if got := Percentile(v, 0.99); got != 10 {
		t.Errorf("p99 = %v, want 10", got)
	}
	if !math.IsNaN(Percentile(nil, 0.5)) {
		t.Error("empty percentile must be NaN")
	}
}

func TestRTTWindowTake(t *testing.T) {
	var w RTTWindow
	if got := w.Take(); got.N != 0 || got.P50MS != nil {
		t.Errorf("empty window = %+v", got)
	}
	for _, ms := range []float64{5, 1, 3} {
		w.Add(ms)
	}
	w.Lost()
	got := w.Take()
	if got.N != 3 || *got.P50MS != 3 || *got.MaxMS != 5 || got.Lost != 1 {
		t.Errorf("window = %+v", got)
	}
	if again := w.Take(); again.N != 0 || again.Lost != 0 {
		t.Errorf("window not cleared: %+v", again)
	}
}

func TestPacketHistogramDeltas(t *testing.T) {
	h := NewPacketHistogram(map[uint32]string{58: "LevelChunk"})
	h.Rx(58)
	h.Rx(58)
	h.Rx(9)
	h.Tx(2000)
	rx, tx := h.Delta()
	if !reflect.DeepEqual(rx, map[string]int64{"LevelChunk": 2, "id_9": 1}) {
		t.Errorf("rx = %v", rx)
	}
	if !reflect.DeepEqual(tx, map[string]int64{"id_1024_plus": 1}) {
		t.Errorf("tx = %v", tx)
	}
	h.Rx(58)
	rx, tx = h.Delta()
	if !reflect.DeepEqual(rx, map[string]int64{"LevelChunk": 1}) || len(tx) != 0 {
		t.Errorf("second delta rx=%v tx=%v", rx, tx)
	}
}

func TestRateLimiter(t *testing.T) {
	r := NewRateLimiter(2, time.Second)
	got := []bool{r.Allow(0), r.Allow(1), r.Allow(2), r.Allow(999), r.Allow(1001), r.Allow(1500), r.Allow(2001)}
	want := []bool{true, true, false, false, true, false, true}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("Allow = %v, want %v", got, want)
	}
}

// fakeClock advances 100 ms per reading.
type fakeClock struct{ n int64 }

func (c *fakeClock) Now() (int64, float64) {
	c.n++
	return 1791813524000 + c.n*100, float64(c.n * 100)
}

func TestEmitterSplicesHeaderAndBody(t *testing.T) {
	var buf bytes.Buffer
	em := NewEmitter(&buf, &fakeClock{})
	em.Emit(EvDial, Dial{Bot: "a<b>", I: 3})
	em.Emit(EvDone, nil)
	em.Emit(EvPkt, struct{}{})
	want := `{"v":1,"ev":"dial","t":1791813524100,"mono_ms":100,"bot":"a<b>","i":3}` + "\n" +
		`{"v":1,"ev":"done","t":1791813524200,"mono_ms":200}` + "\n" +
		`{"v":1,"ev":"pkt","t":1791813524300,"mono_ms":300}` + "\n"
	if buf.String() != want {
		t.Errorf("got\n%s\nwant\n%s", buf.String(), want)
	}
}

type failingWriter struct{}

func (failingWriter) Write([]byte) (int, error) { return 0, errors.New("broken pipe") }

func TestEmitterReportsFirstWriteError(t *testing.T) {
	em := NewEmitter(failingWriter{}, &fakeClock{})
	called := make(chan error, 2)
	em.OnError(func(err error) { called <- err })
	em.Emit(EvDone, nil)
	em.Emit(EvDone, nil)
	select {
	case err := <-called:
		if err == nil || !strings.Contains(err.Error(), "broken pipe") {
			t.Errorf("error = %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("OnError not called")
	}
	select {
	case <-called:
		t.Error("OnError called twice")
	case <-time.After(50 * time.Millisecond):
	}
}

func TestConfigValidate(t *testing.T) {
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	var c Config
	c.Register(fs)
	if err := fs.Parse([]string{"-addr", "127.0.0.1:19672", "-n", "4"}); err != nil {
		t.Fatal(err)
	}
	if err := c.Validate(); err != nil {
		t.Fatalf("defaults rejected: %v", err)
	}
	bad := [][]string{
		{"-n", "0"},
		{"-radius", "0"},
		{"-radius", "97"},
		{"-walk", "zigzag"},
		{"-decode-errors", "ignore"},
		{"-probe-bots", "5"},
		{"-prefix", "a b"},
		{"-spawn-timeout", "0s"},
		{"-stat-interval", "0s"},
	}
	for _, args := range bad {
		fs := flag.NewFlagSet("t", flag.ContinueOnError)
		var c Config
		c.Register(fs)
		if err := fs.Parse(append([]string{"-addr", "x:1", "-n", "4"}, args...)); err != nil {
			t.Fatal(err)
		}
		if err := c.Validate(); err == nil {
			t.Errorf("%v accepted", args)
		}
	}
	var none Config
	none.Register(flag.NewFlagSet("t", flag.ContinueOnError))
	if err := none.Validate(); err == nil || !strings.Contains(err.Error(), "-addr is required") {
		t.Errorf("missing -addr: %v", err)
	}
}

func TestFlagValuesForHello(t *testing.T) {
	fs := flag.NewFlagSet("t", flag.ContinueOnError)
	var c Config
	c.Register(fs)
	_ = fs.Parse([]string{"-addr", "127.0.0.1:1", "-csg", "-stagger", "250ms"})
	got := FlagValues(fs)
	if got["csg"] != true || got["stagger"] != "250ms" || got["n"] != 1 || got["prefix"] != "b" {
		t.Errorf("flags = %v", got)
	}
	if _, ok := got["selftest-output"]; ok {
		t.Error("output-mode flags must not be reported")
	}
	if _, err := json.Marshal(got); err != nil {
		t.Errorf("flags not JSON: %v", err)
	}
}

func TestNameUsesTwoDigits(t *testing.T) {
	c := Config{Prefix: "a"}
	if c.Name(7) != "a07" || c.Name(123) != "a123" {
		t.Errorf("names: %s %s", c.Name(7), c.Name(123))
	}
}
