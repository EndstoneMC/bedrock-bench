// SPDX-License-Identifier: Apache-2.0

package core

import (
	"bytes"
	"encoding/json"
	"io"
	"math"
	"sync"
	"time"
)

// EventVersion is the value of the "v" field of every event (bedrock_bench/schemas/bot-event.schema.json).
const EventVersion = 1

// Event names.
const (
	EvHello       = "hello"
	EvDial        = "dial"
	EvSpawned     = "spawned"
	EvJoinFailed  = "join_failed"
	EvTP          = "tp"
	EvFirstChunk  = "first_chunk"
	EvProgress    = "progress"
	EvStat        = "stat"
	EvPkt         = "pkt"
	EvRTT         = "rtt"
	EvDied        = "died"
	EvRespawned   = "respawned"
	EvDim         = "dim"
	EvLeft        = "left"
	EvDropped     = "dropped"
	EvRejoin      = "rejoin"
	EvKicked      = "kicked"
	EvDecodeError = "decode_error"
	EvAllJoined   = "alljoined"
	EvBotSummary  = "bot_summary"
	EvDone        = "done"
)

// Join stages reported by join_failed.
const (
	StageDial  = "dial"
	StageLogin = "login"
	StageSpawn = "spawn"
)

// Clock stamps events: wall-clock UTC milliseconds and bot-monotonic milliseconds since start.
type Clock interface {
	Now() (epochMS int64, monoMS float64)
}

type systemClock struct{ start time.Time }

// NewSystemClock returns a Clock whose monotonic origin is the moment it is created.
func NewSystemClock() Clock { return systemClock{start: time.Now()} }

func (c systemClock) Now() (int64, float64) {
	now := time.Now()
	return now.UnixMilli(), float64(now.Sub(c.start).Microseconds()) / 1000
}

// Emitter writes events as JSON lines. It is safe for concurrent use; each event is one Write call.
type Emitter struct {
	mu    sync.Mutex
	w     io.Writer
	clock Clock
	buf   bytes.Buffer
	err   error
	onErr func(error)
}

// NewEmitter returns an Emitter writing to w.
func NewEmitter(w io.Writer, clock Clock) *Emitter {
	return &Emitter{w: w, clock: clock}
}

// OnError registers a function called once, with the first write error.
func (e *Emitter) OnError(f func(error)) {
	e.mu.Lock()
	e.onErr = f
	e.mu.Unlock()
}

// Clock returns the clock that stamps events.
func (e *Emitter) Clock() Clock { return e.clock }

// Emit writes one event. body is a struct or map whose JSON object fields follow the header fields v, ev, t and
// mono_ms; nil means no further fields.
func (e *Emitter) Emit(ev string, body any) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.err != nil {
		return
	}
	epoch, mono := e.clock.Now()
	e.buf.Reset()
	enc := json.NewEncoder(&e.buf)
	enc.SetEscapeHTML(false)
	head := struct {
		V      int     `json:"v"`
		Ev     string  `json:"ev"`
		T      int64   `json:"t"`
		MonoMS float64 `json:"mono_ms"`
	}{EventVersion, ev, epoch, mono}
	if err := enc.Encode(head); err != nil {
		e.fail(err)
		return
	}
	if body != nil {
		var rest bytes.Buffer
		benc := json.NewEncoder(&rest)
		benc.SetEscapeHTML(false)
		if err := benc.Encode(body); err != nil {
			e.fail(err)
			return
		}
		fields := bytes.TrimSpace(rest.Bytes())
		if len(fields) > 2 {
			// Splice {"v":..,"mono_ms":..} and {"a":..} into {"v":..,"mono_ms":..,"a":..}.
			e.buf.Truncate(e.buf.Len() - 2)
			e.buf.WriteByte(',')
			e.buf.Write(fields[1:])
			e.buf.WriteByte('\n')
		}
	}
	if _, err := e.w.Write(e.buf.Bytes()); err != nil {
		e.fail(err)
	}
}

func (e *Emitter) fail(err error) {
	e.err = err
	if e.onErr != nil {
		go e.onErr(err)
	}
}

// Err returns the first write or encoding error.
func (e *Emitter) Err() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.err
}

// BuildInfo identifies the bot binary. It is printed by -version and carried in the hello event.
type BuildInfo struct {
	Module       string   `json:"module"`
	Version      string   `json:"version"`
	VCS          string   `json:"vcs,omitempty"`
	Go           string   `json:"go,omitempty"`
	Gophertunnel string   `json:"gophertunnel,omitempty"`
	Patches      []string `json:"patches"`
	Protocol     int      `json:"protocol"`
}

// Intervals are the reporting intervals in milliseconds.
type Intervals struct {
	Progress int64 `json:"progress"`
	Stat     int64 `json:"stat"`
	Pkt      int64 `json:"pkt"`
}

// Hello is the first event of every run.
type Hello struct {
	Build       BuildInfo      `json:"build"`
	Flags       map[string]any `json:"flags"`
	IntervalsMS Intervals      `json:"intervals_ms"`
}

// Dial starts the first join attempt of a bot.
type Dial struct {
	Bot string `json:"bot"`
	I   int    `json:"i"`
}

// Spawned reports a session that reached the world. JoinMS is measured on the bot clock from the attempt start.
type Spawned struct {
	Bot     string  `json:"bot"`
	I       int     `json:"i"`
	Session int     `json:"session"`
	JoinMS  float64 `json:"join_ms"`
	Radius  int32   `json:"radius"`
	Dim     int32   `json:"dim"`
}

// JoinFailed reports one failed join attempt. Final is true when the bot gives up; only final failures of the
// first session count as failed in stat and alljoined.
type JoinFailed struct {
	Bot     string `json:"bot"`
	I       int    `json:"i"`
	Stage   string `json:"stage"`
	Err     string `json:"err"`
	Session int    `json:"session"`
	Attempt int    `json:"attempt"`
	Final   bool   `json:"final"`
}

// TP reports a server teleport of the bot. N counts teleports in the current session.
type TP struct {
	Bot string     `json:"bot"`
	Pos [3]float64 `json:"pos"`
	Dim int32      `json:"dim"`
	N   int        `json:"n"`
}

// FirstChunk is the time from a bot's first teleport to the first chunk inside its view.
type FirstChunk struct {
	Bot  string  `json:"bot"`
	DtMS float64 `json:"dt_ms"`
}

// Progress holds one row per bot: [bot, uniq, in_view, x, y, z, dim, corrections, teleports]. Position and
// dimension are null until the server has teleported the bot.
type Progress struct {
	Rows [][]any `json:"rows"`
}

// Stat holds fleet counters. Everything is cumulative except online and in_view, which are current values.
type Stat struct {
	Online       int64 `json:"online"`
	Joined       int64 `json:"joined"`
	Failed       int64 `json:"failed"`
	Dropped      int64 `json:"dropped"`
	Left         int64 `json:"left"`
	Rejoins      int64 `json:"rejoins"`
	Deaths       int64 `json:"deaths"`
	RxPkts       int64 `json:"rx_pkts"`
	TxPkts       int64 `json:"tx_pkts"`
	RxBytes      int64 `json:"rx_bytes"`
	TxBytes      int64 `json:"tx_bytes"`
	Corrections  int64 `json:"corrections"`
	LevelChunks  int64 `json:"level_chunks"`
	SubChunks    int64 `json:"subchunks"`
	Uniq         int64 `json:"uniq"`
	InView       int64 `json:"in_view"`
	DecodeErrors int64 `json:"decode_errors"`
	Kicked       int64 `json:"kicked"`
	RejoinFailed int64 `json:"rejoin_failed"`
}

// Pkt holds packets received and sent by name since the previous pkt event.
type Pkt struct {
	Rx map[string]int64 `json:"rx"`
	Tx map[string]int64 `json:"tx"`
}

// RTT summarises latency probes since the previous rtt event. Quantiles are null when no probe returned.
type RTT struct {
	N     int      `json:"n"`
	P50MS *float64 `json:"p50_ms"`
	P99MS *float64 `json:"p99_ms"`
	MaxMS *float64 `json:"max_ms"`
	Lost  int64    `json:"lost"`
}

// Lifecycle is the body of died, respawned, dim, left, dropped, rejoin and kicked.
type Lifecycle struct {
	Bot     string `json:"bot"`
	Dim     *int32 `json:"dim,omitempty"`
	Session int    `json:"session,omitempty"`
	Reason  string `json:"reason,omitempty"`
	Err     string `json:"err,omitempty"`
}

// DecodeError reports a packet the bot could not decode. Total is the fleet-wide count so far; events are rate
// limited, the count is not.
type DecodeError struct {
	Bot   *string `json:"bot"`
	PktID *uint32 `json:"pkt_id"`
	Err   string  `json:"err"`
	Total int64   `json:"total"`
}

// AllJoined is emitted once, when every bot has spawned or finally failed its first join.
type AllJoined struct {
	Joined int64 `json:"joined"`
	Failed int64 `json:"failed"`
}

// BotSummary describes the last session of one bot at shutdown. FirstSeenMS holds, for each chunk in view at that
// moment in arrival order, the delta-encoded milliseconds since the bot's first teleport.
type BotSummary struct {
	Bot         string  `json:"bot"`
	Uniq        int     `json:"uniq"`
	InView      int     `json:"in_view"`
	Chunks      int64   `json:"chunks"`
	Corrections int64   `json:"corrections"`
	FirstSeenMS []int64 `json:"first_seen_ms"`
}

// Round3 rounds to three decimals, enough for block coordinates and milliseconds.
func Round3(v float64) float64 {
	return math.Round(v*1000) / 1000
}

// RateLimiter allows a burst of events and then at most one per interval.
type RateLimiter struct {
	mu       sync.Mutex
	burst    int
	interval float64
	used     int
	lastMS   float64
}

// NewRateLimiter returns a limiter that allows burst events, then one per interval.
func NewRateLimiter(burst int, interval time.Duration) *RateLimiter {
	return &RateLimiter{burst: burst, interval: float64(interval.Milliseconds())}
}

// Allow reports whether an event at monoMS may be emitted.
func (r *RateLimiter) Allow(monoMS float64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.used < r.burst {
		r.used++
		r.lastMS = monoMS
		return true
	}
	if monoMS-r.lastMS >= r.interval {
		r.lastMS = monoMS
		return true
	}
	return false
}
