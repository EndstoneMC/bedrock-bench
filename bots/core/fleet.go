// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

// Exit codes of a bot process.
const (
	ExitOK          = 0
	ExitUsage       = 1
	ExitAllFailed   = 2 // every first join failed
	ExitDecodeError = 3 // a decode error with -decode-errors fail
)

// MaxRejoinFailures is how many rejoin attempts in a row may fail before a bot stays offline.
const MaxRejoinFailures = 5

var (
	// RetryDelay is the pause between join attempts and before a rejoin after a drop.
	RetryDelay = 3 * time.Second
	// ShutdownGrace bounds how long sessions get to close at shutdown.
	ShutdownGrace = 10 * time.Second
)

// EndReason says why a session ended.
type EndReason int

const (
	EndJoinFailed EndReason = iota // the attempt never reached the world
	EndStopped                     // the fleet is shutting down
	EndLeft                        // a reconnect or disconnect command closed the session
	EndDropped                     // the connection failed
	EndKicked                      // the server sent a disconnect message
)

// SessionEnd is what Frontend.RunSession returns.
type SessionEnd struct {
	Reason EndReason
	Stage  string // StageDial, StageLogin or StageSpawn for EndJoinFailed
	Err    error  // the cause; for EndKicked the server's message
}

// Frontend is implemented by a protocol module.
type Frontend interface {
	// Build describes the binary.
	Build() BuildInfo
	// PacketNames maps packet ids to the names used in pkt events.
	PacketNames() map[uint32]string
	// RunSession dials, logs in and spawns one session of s.Bot, calls s.Spawned once the bot is in the world,
	// then plays until ctx is done, the connection ends, or s.Leave() is closed. It reports how the session
	// ended; a session that never called Spawned is a failed join attempt.
	RunSession(ctx context.Context, s *Session) SessionEnd
}

// Fleet runs the bots of one process.
type Fleet struct {
	cfg    *Config
	fe     Frontend
	em     *Emitter
	stderr io.Writer

	// C holds the fleet counters; frontends add to it directly (bytes, packets).
	C    Counters
	pkts *PacketHistogram
	rtt  RTTWindow

	bots      []*Bot
	botsMu    sync.Mutex
	jitter    atomic.Bool
	decodeLim *RateLimiter

	ctx      context.Context // cancelled when sessions must close
	cancel   context.CancelFunc
	stopCh   chan struct{} // closed when shutdown is requested
	stopOnce sync.Once
	exit     atomic.Int32
	resolved atomic.Int64
	allOnce  sync.Once
	wg       sync.WaitGroup
}

// NewFleet prepares a fleet. Nothing runs until Run.
func NewFleet(cfg *Config, fe Frontend, em *Emitter, stderr io.Writer) *Fleet {
	f := &Fleet{
		cfg:       cfg,
		fe:        fe,
		em:        em,
		stderr:    stderr,
		pkts:      NewPacketHistogram(fe.PacketNames()),
		decodeLim: NewRateLimiter(10, time.Second),
		stopCh:    make(chan struct{}),
	}
	f.jitter.Store(!cfg.JitterPaused)
	f.ctx, f.cancel = context.WithCancel(context.Background())
	em.OnError(func(err error) {
		f.Logf("", "event output failed, stopping: %v", err)
		f.requestStop(ExitOK)
	})
	return f
}

// Config returns the flag values.
func (f *Fleet) Config() *Config { return f.cfg }

// Emitter returns the event writer.
func (f *Fleet) Emitter() *Emitter { return f.em }

// Logf writes a diagnostic line to stderr. Events go to stdout; stderr is for humans.
func (f *Fleet) Logf(bot, format string, args ...any) {
	if bot != "" {
		format = bot + ": " + format
	}
	fmt.Fprintf(f.stderr, format+"\n", args...)
}

func (f *Fleet) nowMS() float64 {
	_, mono := f.em.Clock().Now()
	return mono
}

func (f *Fleet) stopping() bool {
	select {
	case <-f.stopCh:
		return true
	default:
		return false
	}
}

func (f *Fleet) requestStop(code int32) {
	for {
		cur := f.exit.Load()
		if code <= cur || f.exit.CompareAndSwap(cur, code) {
			break
		}
	}
	f.stopOnce.Do(func() { close(f.stopCh) })
}

// sleep waits for d and reports false when shutdown was requested first.
func (f *Fleet) sleep(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-f.stopCh:
		return false
	}
}

// Run starts the bots, serves stdin and reports until shutdown, and returns the exit code. Shutdown is requested by
// "quit" on stdin, end of stdin, ctx being done (a signal), a decode error under -decode-errors fail, or every
// first join failing.
func (f *Fleet) Run(ctx context.Context, stdin io.Reader, flags map[string]any) int {
	build := f.fe.Build()
	if build.Patches == nil {
		build.Patches = []string{}
	}
	if flags == nil {
		flags = map[string]any{}
	}
	f.em.Emit(EvHello, Hello{Build: build, Flags: flags, IntervalsMS: f.cfg.Intervals()})

	go f.readStdin(stdin)
	go func() {
		select {
		case <-ctx.Done():
			f.requestStop(ExitOK)
		case <-f.stopCh:
		}
	}()
	reportDone := make(chan struct{})
	go f.report(reportDone)

	for i := 0; i < f.cfg.N && !f.stopping(); i++ {
		b := f.newBot(i)
		f.wg.Add(1)
		go f.runBot(b)
		if i < f.cfg.N-1 && !f.sleep(f.cfg.Stagger) {
			break
		}
	}

	<-f.stopCh
	close(reportDone)
	f.emitProgress()
	f.emitStat()
	f.emitPkt()
	if f.cfg.ProbeBots > 0 {
		f.em.Emit(EvRTT, f.rtt.Take())
	}

	f.cancel()
	waited := make(chan struct{})
	go func() {
		f.wg.Wait()
		close(waited)
	}()
	select {
	case <-waited:
	case <-time.After(ShutdownGrace):
		f.Logf("", "sessions did not close within %v", ShutdownGrace)
	}

	for _, b := range f.snapshotBots() {
		if s := b.lastSpawned(); s != nil {
			f.em.Emit(EvBotSummary, s.summary())
		}
	}
	if f.C.Joined.Load() == 0 && f.C.Failed.Load() == int64(f.cfg.N) {
		f.requestStop(ExitAllFailed)
	}
	f.em.Emit(EvDone, nil)
	return int(f.exit.Load())
}

func (f *Fleet) newBot(i int) *Bot {
	name := f.cfg.Name(i)
	b := &Bot{Index: i, Name: name, UUID: BotUUID(f.cfg.IdentitySeed, name), fleet: f, wake: make(chan int, 1)}
	if f.cfg.FakeXUID {
		b.XUID = FakeXUID(i)
	}
	f.botsMu.Lock()
	f.bots = append(f.bots, b)
	f.botsMu.Unlock()
	return b
}

func (f *Fleet) snapshotBots() []*Bot {
	f.botsMu.Lock()
	defer f.botsMu.Unlock()
	return append([]*Bot(nil), f.bots...)
}

func (f *Fleet) findBot(c Command) *Bot {
	for _, b := range f.snapshotBots() {
		if (c.I != nil && b.Index == *c.I) || (c.Bot != "" && b.Name == c.Bot) {
			return b
		}
	}
	return nil
}

// resolve marks one bot's first join as decided and emits alljoined after the last one.
func (f *Fleet) resolve() {
	if f.resolved.Add(1) != int64(f.cfg.N) {
		return
	}
	f.allOnce.Do(func() {
		joined, failed := f.C.Joined.Load(), f.C.Failed.Load()
		f.em.Emit(EvAllJoined, AllJoined{Joined: joined, Failed: failed})
		if joined == 0 {
			f.requestStop(ExitAllFailed)
		}
	})
}

func (f *Fleet) runBot(b *Bot) {
	defer f.wg.Done()
	cfg := f.cfg
	number, fails := 1, 0
	for !f.stopping() {
		s := newSession(b, number)
		b.setCurrent(s)
		if number == 1 {
			f.em.Emit(EvDial, Dial{Bot: b.Name, I: b.Index})
		} else {
			f.em.Emit(EvRejoin, Lifecycle{Bot: b.Name, Session: number})
		}
		end := f.fe.RunSession(f.ctx, s)

		if !s.isSpawned() {
			if f.stopping() {
				return
			}
			fails++
			final := (number == 1 && fails > cfg.JoinRetries) || (number > 1 && fails >= MaxRejoinFailures)
			stage := end.Stage
			if stage == "" {
				stage = StageDial
			}
			f.em.Emit(EvJoinFailed, JoinFailed{
				Bot: b.Name, I: b.Index, Stage: stage, Err: errString(end.Err),
				Session: number, Attempt: fails, Final: final,
			})
			if final {
				if number == 1 {
					f.C.Failed.Add(1)
					f.resolve()
				} else {
					f.C.RejoinFailed.Add(1)
				}
				return
			}
			if !f.sleep(RetryDelay) {
				return
			}
			continue
		}

		fails = 0
		f.C.Online.Add(-1)
		b.setOnline(false)
		switch end.Reason {
		case EndStopped, EndJoinFailed:
			return
		case EndLeft:
			f.C.Left.Add(1)
			f.em.Emit(EvLeft, Lifecycle{Bot: b.Name, Session: number})
			rejoin, delay := s.leaveIntent()
			if !rejoin {
				var ok bool
				if delay, ok = b.waitWake(); !ok {
					return
				}
			}
			if !f.sleep(time.Duration(delay) * time.Millisecond) {
				return
			}
			number++
			continue
		case EndKicked:
			f.C.Dropped.Add(1)
			f.C.Kicked.Add(1)
			f.em.Emit(EvKicked, Lifecycle{Bot: b.Name, Session: number, Reason: errString(end.Err)})
		case EndDropped:
			f.C.Dropped.Add(1)
			f.em.Emit(EvDropped, Lifecycle{Bot: b.Name, Session: number, Err: errString(end.Err)})
		}
		if !cfg.RejoinOnDrop || !f.sleep(RetryDelay) {
			return
		}
		number++
	}
}

func errString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func (f *Fleet) readStdin(r io.Reader) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	for sc.Scan() {
		c, err := ParseCommand(sc.Text(), f.cfg.Control)
		if errors.Is(err, ErrEmptyLine) {
			continue
		}
		if err != nil {
			f.Logf("", "ignored: %v", err)
			continue
		}
		f.apply(c)
		if c.Op == OpQuit {
			return
		}
	}
	if err := sc.Err(); err != nil {
		f.Logf("", "stdin: %v", err)
	}
	f.requestStop(ExitOK)
}

func (f *Fleet) apply(c Command) {
	switch c.Op {
	case OpQuit:
		f.requestStop(ExitOK)
	case OpJitter:
		f.jitter.Store(*c.On)
	case OpRadius:
		if !c.Targets() {
			for _, b := range f.snapshotBots() {
				b.request(c)
			}
			return
		}
		if b := f.findBot(c); b != nil {
			b.request(c)
		} else {
			f.Logf("", "radius: no such bot")
		}
	case OpReconnect, OpDisconnect:
		b := f.findBot(c)
		if b == nil {
			f.Logf("", "%s: no such bot", c.Op)
			return
		}
		delay := DefaultRejoinDelayMS
		if c.DelayMS != nil {
			delay = *c.DelayMS
		}
		b.leaveOrWake(c.Op == OpReconnect, delay)
	}
}

func (f *Fleet) report(done <-chan struct{}) {
	progress := time.NewTicker(f.cfg.ProgressInterval)
	stat := time.NewTicker(f.cfg.StatInterval)
	pkt := time.NewTicker(f.cfg.PktInterval)
	defer progress.Stop()
	defer stat.Stop()
	defer pkt.Stop()
	for {
		select {
		case <-done:
			return
		case <-progress.C:
			f.emitProgress()
		case <-stat.C:
			f.emitStat()
			if f.cfg.ProbeBots > 0 {
				f.em.Emit(EvRTT, f.rtt.Take())
			}
		case <-pkt.C:
			f.emitPkt()
		}
	}
}

func (f *Fleet) emitProgress() {
	bots := f.snapshotBots()
	rows := make([][]any, 0, len(bots))
	for _, b := range bots {
		rows = append(rows, b.row())
	}
	f.em.Emit(EvProgress, Progress{Rows: rows})
}

func (f *Fleet) emitStat() {
	var inView int64
	for _, b := range f.snapshotBots() {
		if s := b.onlineSession(); s != nil {
			inView += int64(s.inView())
		}
	}
	c := &f.C
	f.em.Emit(EvStat, Stat{
		Online: c.Online.Load(), Joined: c.Joined.Load(), Failed: c.Failed.Load(), Dropped: c.Dropped.Load(),
		Left: c.Left.Load(), Rejoins: c.Rejoins.Load(), Deaths: c.Deaths.Load(),
		RxPkts: c.RxPkts.Load(), TxPkts: c.TxPkts.Load(), RxBytes: c.RxBytes.Load(), TxBytes: c.TxBytes.Load(),
		Corrections: c.Corrections.Load(), LevelChunks: c.LevelChunks.Load(), SubChunks: c.SubChunks.Load(),
		Uniq: c.Uniq.Load(), InView: inView, DecodeErrors: c.DecodeErrors.Load(),
		Kicked: c.Kicked.Load(), RejoinFailed: c.RejoinFailed.Load(),
	})
}

func (f *Fleet) emitPkt() {
	rx, tx := f.pkts.Delta()
	f.em.Emit(EvPkt, Pkt{Rx: rx, Tx: tx})
}

// Bot is one simulated player. It outlives its sessions.
type Bot struct {
	Index int
	Name  string
	UUID  string
	XUID  string // empty unless -fake-xuid

	fleet   *Fleet
	mu      sync.Mutex
	current *Session // the session being joined or played
	spawned *Session // the last session that reached the world
	online  bool
	waiting bool     // offline after a disconnect command, waiting for a reconnect
	wake    chan int // rejoin delay sent by a reconnect while waiting
}

func (b *Bot) setCurrent(s *Session) {
	b.mu.Lock()
	b.current = s
	b.mu.Unlock()
}

func (b *Bot) setOnline(online bool) {
	b.mu.Lock()
	b.online = online
	b.mu.Unlock()
}

func (b *Bot) lastSpawned() *Session {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.spawned
}

func (b *Bot) onlineSession() *Session {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.online {
		return nil
	}
	return b.current
}

func (b *Bot) row() []any {
	b.mu.Lock()
	s := b.spawned
	b.mu.Unlock()
	if s == nil {
		return []any{b.Name, 0, 0, nil, nil, nil, nil, 0, 0}
	}
	return s.row()
}

func (b *Bot) request(c Command) {
	if s := b.onlineSession(); s != nil {
		select {
		case s.requests <- c:
		default:
			b.fleet.Logf(b.Name, "control request dropped, previous one still pending")
		}
	}
}

func (b *Bot) leaveOrWake(rejoin bool, delayMS int) {
	b.mu.Lock()
	online, waiting, s := b.online, b.waiting, b.current
	b.mu.Unlock()
	switch {
	case online:
		s.requestLeave(rejoin, delayMS)
	case waiting && rejoin:
		select {
		case b.wake <- delayMS:
		default:
		}
	default:
		b.fleet.Logf(b.Name, "not online, command ignored")
	}
}

func (b *Bot) waitWake() (int, bool) {
	b.mu.Lock()
	b.waiting = true
	b.mu.Unlock()
	defer func() {
		b.mu.Lock()
		b.waiting = false
		b.mu.Unlock()
	}()
	select {
	case d := <-b.wake:
		return d, true
	case <-b.fleet.stopCh:
		return 0, false
	}
}

// Session is one connection of a bot, from the join attempt to its end. Frontends call its methods from any
// goroutine.
type Session struct {
	Bot    *Bot
	Number int

	fleet    *Fleet
	startMS  float64
	leave    chan struct{}
	requests chan Command

	mu            sync.Mutex
	spawned       bool
	x, y, z       float64
	dim           int32
	radius        int32
	placed        bool
	teleports     int
	corrections   int64
	chunks        *ChunkTracker
	firstChunk    bool
	leaveOnce     sync.Once
	rejoin        bool
	rejoinDelayMS int
}

func newSession(b *Bot, number int) *Session {
	return &Session{
		Bot:      b,
		Number:   number,
		fleet:    b.fleet,
		startMS:  b.fleet.nowMS(),
		leave:    make(chan struct{}),
		requests: make(chan Command, 4),
		chunks:   NewChunkTracker(),
	}
}

// Config returns the flag values.
func (s *Session) Config() *Config { return s.fleet.cfg }

// Fleet returns the fleet the session belongs to.
func (s *Session) Fleet() *Fleet { return s.fleet }

// Logf writes a diagnostic line for this bot to stderr.
func (s *Session) Logf(format string, args ...any) { s.fleet.Logf(s.Bot.Name, format, args...) }

// Leave is closed when a control command asks the session to end.
func (s *Session) Leave() <-chan struct{} { return s.leave }

// Requests delivers radius commands for this bot.
func (s *Session) Requests() <-chan Command { return s.requests }

// JitterOn reports whether radius jitter is currently enabled.
func (s *Session) JitterOn() bool { return s.fleet.jitter.Load() }

// Probing reports whether this bot sends latency probes.
func (s *Session) Probing() bool { return s.Bot.Index < s.fleet.cfg.ProbeBots }

func (s *Session) isSpawned() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.spawned
}

func (s *Session) requestLeave(rejoin bool, delayMS int) {
	s.leaveOnce.Do(func() {
		s.mu.Lock()
		s.rejoin, s.rejoinDelayMS = rejoin, delayMS
		s.mu.Unlock()
		close(s.leave)
	})
}

func (s *Session) leaveIntent() (bool, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.rejoin, s.rejoinDelayMS
}

// Spawned reports that the bot reached the world at the given position with the server's chunk radius.
func (s *Session) Spawned(x, y, z float64, dim, radius int32) {
	now := s.fleet.nowMS()
	s.mu.Lock()
	s.spawned = true
	s.x, s.y, s.z, s.dim, s.radius = x, y, z, dim, radius
	s.mu.Unlock()

	b := s.Bot
	b.mu.Lock()
	b.spawned = s
	b.online = true
	b.mu.Unlock()
	s.fleet.C.Online.Add(1)
	s.fleet.em.Emit(EvSpawned, Spawned{
		Bot: b.Name, I: b.Index, Session: s.Number, JoinMS: Round3(math.Max(0, now-s.startMS)),
		Radius: radius, Dim: dim,
	})
	if s.Number == 1 {
		s.fleet.C.Joined.Add(1)
		s.fleet.resolve()
	} else {
		s.fleet.C.Rejoins.Add(1)
	}
}

// SetRadius records the chunk radius the server granted.
func (s *Session) SetRadius(r int32) {
	s.mu.Lock()
	s.radius = r
	s.mu.Unlock()
}

// LevelChunk records a chunk packet for column (cx, cz) of dimension dim.
func (s *Session) LevelChunk(dim, cx, cz int32) {
	now := s.fleet.nowMS()
	s.fleet.C.LevelChunks.Add(1)
	key := ChunkKey{Dim: dim, X: cx, Z: cz}
	s.mu.Lock()
	isNew := s.chunks.Add(key, now)
	first := false
	if s.chunks.Started() && !s.firstChunk && InView(key, s.dim, BlockToChunk(s.x), BlockToChunk(s.z), s.radius) {
		s.firstChunk, first = true, true
	}
	origin := s.chunks.OriginMS()
	s.mu.Unlock()
	if isNew {
		s.fleet.C.Uniq.Add(1)
	}
	if first {
		s.fleet.em.Emit(EvFirstChunk, FirstChunk{Bot: s.Bot.Name, DtMS: Round3(math.Max(0, now-origin))})
	}
}

// SubChunks records n sub-chunk entries received.
func (s *Session) SubChunks(n int) { s.fleet.C.SubChunks.Add(int64(n)) }

// Teleported records a server teleport of the bot.
func (s *Session) Teleported(x, y, z float64) {
	now := s.fleet.nowMS()
	s.mu.Lock()
	s.x, s.y, s.z = x, y, z
	s.placed = true
	s.teleports++
	s.chunks.Start(now)
	ev := TP{Bot: s.Bot.Name, Pos: [3]float64{Round3(x), Round3(y), Round3(z)}, Dim: s.dim, N: s.teleports}
	s.mu.Unlock()
	s.fleet.em.Emit(EvTP, ev)
}

// Moved records a position the server set without a teleport.
func (s *Session) Moved(x, y, z float64) {
	s.mu.Lock()
	s.x, s.y, s.z = x, y, z
	s.mu.Unlock()
}

// Corrected records a movement correction from the server.
func (s *Session) Corrected(x, y, z float64) {
	s.fleet.C.Corrections.Add(1)
	s.mu.Lock()
	s.x, s.y, s.z = x, y, z
	s.corrections++
	s.mu.Unlock()
}

// ChangedDimension records a dimension change.
func (s *Session) ChangedDimension(dim int32, x, y, z float64) {
	s.mu.Lock()
	s.dim = dim
	s.x, s.y, s.z = x, y, z
	s.mu.Unlock()
	s.fleet.em.Emit(EvDim, Lifecycle{Bot: s.Bot.Name, Dim: &dim, Session: s.Number})
}

// Died records the bot's death.
func (s *Session) Died() {
	s.fleet.C.Deaths.Add(1)
	s.fleet.em.Emit(EvDied, Lifecycle{Bot: s.Bot.Name, Session: s.Number})
}

// Respawned records the bot coming back after a death.
func (s *Session) Respawned() {
	s.fleet.em.Emit(EvRespawned, Lifecycle{Bot: s.Bot.Name, Session: s.Number})
}

// Advance moves the bot by (dx, dz) and returns the new position.
func (s *Session) Advance(dx, dz float64) (x, y, z float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.x += dx
	s.z += dz
	return s.x, s.y, s.z
}

// Position returns the bot's position and dimension as the bot believes them.
func (s *Session) Position() (x, y, z float64, dim int32) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.x, s.y, s.z, s.dim
}

// RxPacket counts one received packet.
func (s *Session) RxPacket(id uint32) {
	s.fleet.C.RxPkts.Add(1)
	s.fleet.pkts.Rx(id)
}

// TxPacket counts one sent packet.
func (s *Session) TxPacket(id uint32) {
	s.fleet.C.TxPkts.Add(1)
	s.fleet.pkts.Tx(id)
}

// RTT records one probe round trip.
func (s *Session) RTT(d time.Duration) { s.fleet.rtt.Add(float64(d.Microseconds()) / 1000) }

// ProbeLost records a probe that got no reply.
func (s *Session) ProbeLost() { s.fleet.rtt.Lost() }

// DecodeError records a packet the bot could not decode. pktID is nil when the packet is not known. Under
// -decode-errors fail the fleet shuts down with exit code 3.
func (s *Session) DecodeError(pktID *uint32, msg string) {
	total := s.fleet.C.DecodeErrors.Add(1)
	if s.fleet.decodeLim.Allow(s.fleet.nowMS()) {
		name := s.Bot.Name
		s.fleet.em.Emit(EvDecodeError, DecodeError{Bot: &name, PktID: pktID, Err: msg, Total: total})
	}
	if s.fleet.cfg.DecodeErrors == DecodeFail {
		s.fleet.requestStop(ExitDecodeError)
	}
}

func (s *Session) inView() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.chunks.InView(s.dim, BlockToChunk(s.x), BlockToChunk(s.z), s.radius)
}

func (s *Session) row() []any {
	s.mu.Lock()
	defer s.mu.Unlock()
	inView := s.chunks.InView(s.dim, BlockToChunk(s.x), BlockToChunk(s.z), s.radius)
	if !s.placed {
		return []any{s.Bot.Name, s.chunks.Uniq(), inView, nil, nil, nil, nil, s.corrections, s.teleports}
	}
	return []any{s.Bot.Name, s.chunks.Uniq(), inView, Round3(s.x), Round3(s.y), Round3(s.z), s.dim, s.corrections, s.teleports}
}

func (s *Session) summary() BotSummary {
	s.mu.Lock()
	defer s.mu.Unlock()
	cx, cz := BlockToChunk(s.x), BlockToChunk(s.z)
	return BotSummary{
		Bot:         s.Bot.Name,
		Uniq:        s.chunks.Uniq(),
		InView:      s.chunks.InView(s.dim, cx, cz, s.radius),
		Chunks:      s.chunks.Received(),
		Corrections: s.corrections,
		FirstSeenMS: s.chunks.FirstSeenDeltas(s.dim, cx, cz, s.radius),
	}
}
