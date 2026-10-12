// SPDX-License-Identifier: Apache-2.0

package core

import (
	"math"
	"sort"
	"strconv"
	"sync"
	"sync/atomic"
)

// Counters are the fleet-wide totals behind the stat event.
type Counters struct {
	Online       atomic.Int64
	Joined       atomic.Int64
	Failed       atomic.Int64
	Dropped      atomic.Int64 // sessions that ended without the bot asking, kicks included
	Kicked       atomic.Int64 // sessions ended by a server disconnect message
	Left         atomic.Int64 // sessions ended by a reconnect or disconnect command
	Rejoins      atomic.Int64
	RejoinFailed atomic.Int64
	Deaths       atomic.Int64
	RxPkts       atomic.Int64
	TxPkts       atomic.Int64
	RxBytes      atomic.Int64 // UDP payload bytes received
	TxBytes      atomic.Int64 // UDP payload bytes sent
	Corrections  atomic.Int64
	LevelChunks  atomic.Int64 // every chunk packet, before and after teleports
	SubChunks    atomic.Int64 // sub-chunk entries received
	Uniq         atomic.Int64 // first sightings of a chunk after the first teleport, summed over sessions
	DecodeErrors atomic.Int64
}

// MaxPacketID bounds the per-id packet counters; this id and larger ones share one bucket.
const MaxPacketID = 1024

// PacketHistogram counts packets by id and reports deltas by name.
type PacketHistogram struct {
	rx, tx         [MaxPacketID + 1]atomic.Int64
	mu             sync.Mutex
	lastRx, lastTx [MaxPacketID + 1]int64
	names          map[uint32]string
}

// NewPacketHistogram returns a histogram that names packet ids with names; unknown ids are reported as "id_<n>".
func NewPacketHistogram(names map[uint32]string) *PacketHistogram {
	return &PacketHistogram{names: names}
}

func bucket(id uint32) uint32 {
	if id >= MaxPacketID {
		return MaxPacketID
	}
	return id
}

// Rx counts one received packet.
func (h *PacketHistogram) Rx(id uint32) { h.rx[bucket(id)].Add(1) }

// Tx counts one sent packet.
func (h *PacketHistogram) Tx(id uint32) { h.tx[bucket(id)].Add(1) }

// Name is the name of a packet id.
func (h *PacketHistogram) Name(id uint32) string {
	if id >= MaxPacketID {
		return "id_" + strconv.Itoa(MaxPacketID) + "_plus"
	}
	if n, ok := h.names[id]; ok {
		return n
	}
	return "id_" + strconv.FormatUint(uint64(id), 10)
}

// Delta returns the packets counted since the previous call, by name, leaving out zeros.
func (h *PacketHistogram) Delta() (rx, tx map[string]int64) {
	h.mu.Lock()
	defer h.mu.Unlock()
	rx, tx = map[string]int64{}, map[string]int64{}
	for id := range h.rx {
		if c := h.rx[id].Load(); c != h.lastRx[id] {
			rx[h.Name(uint32(id))] += c - h.lastRx[id]
			h.lastRx[id] = c
		}
		if c := h.tx[id].Load(); c != h.lastTx[id] {
			tx[h.Name(uint32(id))] += c - h.lastTx[id]
			h.lastTx[id] = c
		}
	}
	return rx, tx
}

// Percentile is the nearest-rank quantile of sorted values: sorted[round(q·(n-1))].
func Percentile(sorted []float64, q float64) float64 {
	if len(sorted) == 0 {
		return math.NaN()
	}
	return sorted[int(math.Round(q*float64(len(sorted)-1)))]
}

// RTTWindow collects probe round trips between two rtt events.
type RTTWindow struct {
	mu      sync.Mutex
	samples []float64
	lost    int64
}

// Add records one round trip in milliseconds.
func (w *RTTWindow) Add(ms float64) {
	w.mu.Lock()
	w.samples = append(w.samples, ms)
	w.mu.Unlock()
}

// Lost records a probe that got no reply before the next one was due.
func (w *RTTWindow) Lost() {
	w.mu.Lock()
	w.lost++
	w.mu.Unlock()
}

// Take summarises and clears the window.
func (w *RTTWindow) Take() RTT {
	w.mu.Lock()
	samples, lost := w.samples, w.lost
	w.samples, w.lost = nil, 0
	w.mu.Unlock()
	out := RTT{N: len(samples), Lost: lost}
	if len(samples) == 0 {
		return out
	}
	sort.Float64s(samples)
	p50, p99, mx := Round3(Percentile(samples, 0.5)), Round3(Percentile(samples, 0.99)), Round3(samples[len(samples)-1])
	out.P50MS, out.P99MS, out.MaxMS = &p50, &p99, &mx
	return out
}
