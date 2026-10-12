// SPDX-License-Identifier: Apache-2.0

package core

import "math"

// ChunkKey identifies a chunk column. The dimension is part of the key, so the same coordinates in two
// dimensions are two chunks.
type ChunkKey struct {
	Dim  int32
	X, Z int32
}

// BlockToChunk converts a block coordinate to a chunk coordinate.
func BlockToChunk(v float64) int32 {
	return int32(math.Floor(v / 16))
}

// InView reports whether chunk k is within radius+1 chunks (Euclidean, in chunk units) of the chunk (cx, cz) in
// dimension dim. The extra ring tolerates rounding at the edge of the server's disc; chunks of an old view that a
// server keeps streaming after a teleport fall outside it.
func InView(k ChunkKey, dim, cx, cz, radius int32) bool {
	if k.Dim != dim {
		return false
	}
	dx, dz := int64(k.X-cx), int64(k.Z-cz)
	r := int64(radius) + 1
	return dx*dx+dz*dz <= r*r
}

// ChunkTracker records the chunks one bot session receives after its first teleport. Chunks that arrive before
// it belong to the spawn area and are not counted.
type ChunkTracker struct {
	started  bool
	originMS float64
	seen     map[ChunkKey]float64 // first-seen time, ms since the first teleport
	order    []ChunkKey           // arrival order of first sightings
	received int64                // chunk packets after the first teleport, resends included
}

// NewChunkTracker returns an empty tracker.
func NewChunkTracker() *ChunkTracker {
	return &ChunkTracker{seen: map[ChunkKey]float64{}}
}

// Start begins tracking at the first teleport. Later calls do nothing and return false.
func (t *ChunkTracker) Start(nowMS float64) bool {
	if t.started {
		return false
	}
	t.started = true
	t.originMS = nowMS
	return true
}

// Started reports whether the first teleport has happened.
func (t *ChunkTracker) Started() bool { return t.started }

// OriginMS is the bot-monotonic time of the first teleport.
func (t *ChunkTracker) OriginMS() float64 { return t.originMS }

// Add records a chunk packet received at nowMS and reports whether it is the first sighting of that chunk.
// Before Start it does nothing.
func (t *ChunkTracker) Add(k ChunkKey, nowMS float64) bool {
	if !t.started {
		return false
	}
	t.received++
	if _, ok := t.seen[k]; ok {
		return false
	}
	t.seen[k] = math.Max(0, nowMS-t.originMS)
	t.order = append(t.order, k)
	return true
}

// Uniq is the number of distinct chunks received since the first teleport.
func (t *ChunkTracker) Uniq() int { return len(t.seen) }

// Received is the number of chunk packets received since the first teleport.
func (t *ChunkTracker) Received() int64 { return t.received }

// InView counts the distinct chunks inside the view of a bot standing in chunk (cx, cz) of dimension dim.
func (t *ChunkTracker) InView(dim, cx, cz, radius int32) int {
	n := 0
	for k := range t.seen {
		if InView(k, dim, cx, cz, radius) {
			n++
		}
	}
	return n
}

// FirstSeenDeltas returns, for the chunks currently in view in arrival order, the first-seen times in whole
// milliseconds since the first teleport, delta-encoded: the first value is absolute, each later one is the gap to
// the previous. Every value is non-negative.
func (t *ChunkTracker) FirstSeenDeltas(dim, cx, cz, radius int32) []int64 {
	out := []int64{}
	var prev int64
	for _, k := range t.order {
		if !InView(k, dim, cx, cz, radius) {
			continue
		}
		ms := int64(math.Floor(t.seen[k]))
		if ms < prev {
			ms = prev
		}
		out = append(out, ms-prev)
		prev = ms
	}
	return out
}
