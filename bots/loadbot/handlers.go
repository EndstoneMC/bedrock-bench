// SPDX-License-Identifier: Apache-2.0

package main

import (
	"time"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

const (
	// respawnDelay is how long a dead bot waits before asking to respawn.
	respawnDelay = time.Second
	// respawnRetry is the pause between respawn requests while the bot stays dead.
	respawnRetry = 10 * time.Second
	// respawnAttempts bounds the respawn requests per death.
	respawnAttempts = 4
	// dimensionAckDelay is the pause between a dimension change and its acknowledgement.
	dimensionAckDelay = time.Second
)

// handle translates one received packet into core accounting and the replies a game client sends.
func (b *botConn) handle(pk packet.Packet) {
	switch p := pk.(type) {
	case *packet.LevelChunk:
		b.s.LevelChunk(p.Dimension, p.Position[0], p.Position[1])
		if len(p.BlobHashes) > 0 {
			// Every blob is a miss: the server sends full chunk data, the worst case it can face.
			b.write(&packet.ClientCacheBlobStatus{MissHashes: p.BlobHashes})
		}
		if limit, ok := p.SubChunkLimit.Value(); ok {
			b.write(&packet.SubChunkRequest{
				Dimension: p.Dimension,
				Position:  protocol.SubChunkPos{p.Position[0], 0, p.Position[1]},
				Offsets:   subChunkOffsets(p.Dimension, limit),
			})
		}
	case *packet.SubChunk:
		b.s.SubChunks(len(p.SubChunkEntries))
		var miss []uint64
		for _, e := range p.SubChunkEntries {
			if h, ok := e.BlobHash.Value(); ok {
				miss = append(miss, h)
			}
		}
		if len(miss) > 0 {
			b.write(&packet.ClientCacheBlobStatus{MissHashes: miss})
		}
	case *packet.ChunkRadiusUpdated:
		b.s.SetRadius(p.ChunkRadius)
	case *packet.NetworkStackLatency:
		if rtt, ok := b.probe.take(p.Timestamp, time.Now()); ok {
			b.s.RTT(rtt)
			return
		}
		if p.NeedsResponse {
			b.write(&packet.NetworkStackLatency{Timestamp: p.Timestamp})
		}
	case *packet.CorrectPlayerMovePrediction:
		b.s.Corrected(float64(p.Position[0]), float64(p.Position[1]), float64(p.Position[2]))
	case *packet.MovePlayer:
		if p.EntityRuntimeID != b.gd.EntityRuntimeID {
			return
		}
		x, y, z := float64(p.Position[0]), float64(p.Position[1]), float64(p.Position[2])
		switch p.Mode {
		case packet.MoveModeTeleport:
			b.s.Teleported(x, y, z)
			b.handledTeleport.Store(true)
		case packet.MoveModeReset:
			b.s.Moved(x, y, z)
			b.handledTeleport.Store(true)
		default:
			b.s.Moved(x, y, z)
		}
	case *packet.ChangeDimension:
		b.changeDimension(p)
	case *packet.SetHealth:
		b.health(float32(p.Health))
	case *packet.UpdateAttributes:
		if p.EntityRuntimeID != b.gd.EntityRuntimeID {
			return
		}
		for _, a := range p.Attributes {
			if a.Name == "minecraft:health" {
				b.health(a.Value)
			}
		}
	case *packet.ActorEvent:
		if p.EntityRuntimeID == b.gd.EntityRuntimeID && p.EventType == packet.ActorEventDeath {
			b.died()
		}
	case *packet.Respawn:
		if p.State == packet.RespawnStateReadyToSpawn {
			b.alive()
		}
	}
}

func subChunkOffsets(dim int32, limit int32) []protocol.SubChunkOffset {
	ys := core.SubChunkYOffsets(dim, limit)
	out := make([]protocol.SubChunkOffset, len(ys))
	for i, y := range ys {
		out[i] = protocol.SubChunkOffset{0, y, 0}
	}
	return out
}

func (b *botConn) health(h float32) {
	if h <= 0 {
		b.died()
	} else {
		b.alive()
	}
}

// died marks the bot dead once per death and starts asking to respawn.
func (b *botConn) died() {
	if !b.dead.CompareAndSwap(false, true) {
		return
	}
	b.respawnSent.Store(false)
	b.s.Died()
	time.AfterFunc(respawnDelay, func() { b.requestRespawn(1) })
}

// alive marks a dead bot respawned. Health updates before the bot asked to respawn do not count.
func (b *botConn) alive() {
	if b.respawnSent.Load() && b.dead.CompareAndSwap(true, false) {
		b.respawnSent.Store(false)
		b.s.Respawned()
	}
}

func (b *botConn) requestRespawn(attempt int) {
	select {
	case <-b.done:
		return
	default:
	}
	if !b.dead.Load() {
		return
	}
	b.respawnSent.Store(true)
	b.write(&packet.Respawn{State: packet.RespawnStateClientReadyToSpawn, EntityRuntimeID: b.gd.EntityRuntimeID})
	b.write(&packet.PlayerAction{EntityRuntimeID: b.gd.EntityRuntimeID, ActionType: protocol.PlayerActionRespawn, BlockFace: -1})
	_ = b.conn.Flush()
	if attempt < respawnAttempts {
		time.AfterFunc(respawnRetry, func() { b.requestRespawn(attempt + 1) })
	}
}

// changeDimension records the move and acknowledges it the way a client does once the new dimension is loaded.
func (b *botConn) changeDimension(p *packet.ChangeDimension) {
	b.s.ChangedDimension(p.Dimension, float64(p.Position[0]), float64(p.Position[1]), float64(p.Position[2]))
	screen := p.LoadingScreenID
	if b.cfg.LoadScreen {
		b.write(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeStart, LoadingScreenID: screen})
		_ = b.conn.Flush()
	}
	time.AfterFunc(dimensionAckDelay, func() {
		select {
		case <-b.done:
			return
		default:
		}
		b.write(&packet.PlayerAction{
			EntityRuntimeID: b.gd.EntityRuntimeID, ActionType: protocol.PlayerActionDimensionChangeDone, BlockFace: -1,
		})
		if b.cfg.LoadScreen {
			b.write(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeEnd, LoadingScreenID: screen})
		}
		_ = b.conn.Flush()
		b.handledTeleport.Store(true)
	})
}
