// SPDX-License-Identifier: Apache-2.0

package main

import (
	"sync"
	"time"

	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// probeInterval is how often a probe bot sends a latency probe.
const probeInterval = time.Second

// prober measures round trips with NetworkStackLatency packets that ask for a response. One probe is in flight at
// a time; a probe still unanswered when the next is due counts as lost.
type prober struct {
	mu      sync.Mutex
	pending bool
	stamp   int64
	sentAt  time.Time
}

// next records a new probe and reports whether the previous one was lost.
func (p *prober) next(now time.Time) (stamp int64, lost bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	lost = p.pending
	p.pending = true
	p.stamp = now.UnixMicro()
	p.sentAt = now
	return p.stamp, lost
}

// take matches an echoed timestamp against the probe in flight and returns the round trip. Servers echo the
// value as sent or scaled by 1000, so all three forms match.
func (p *prober) take(stamp int64, now time.Time) (time.Duration, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.pending || !(stamp == p.stamp || stamp == p.stamp*1000 || stamp*1000 == p.stamp) {
		return 0, false
	}
	p.pending = false
	return now.Sub(p.sentAt), true
}

func (b *botConn) probeLoop() {
	t := time.NewTicker(probeInterval)
	defer t.Stop()
	for {
		select {
		case <-b.done:
			return
		case now := <-t.C:
			stamp, lost := b.probe.next(now)
			if lost {
				b.s.ProbeLost()
			}
			b.write(&packet.NetworkStackLatency{Timestamp: stamp, NeedsResponse: true})
			_ = b.conn.Flush()
		}
	}
}
