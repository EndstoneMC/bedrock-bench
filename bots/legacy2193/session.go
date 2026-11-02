// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"errors"
	"log/slog"
	"net"
	"sync/atomic"
	"time"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/df-mc/go-nethernet"
	"github.com/df-mc/go-nethernet/endpoint"
	"github.com/go-gl/mathgl/mgl32"
	"github.com/google/uuid"
	"github.com/sandertv/gophertunnel/minecraft"
	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/login"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// defaultRequestedRadius is the radius gophertunnel requests during spawn; -radius sends another request only
// when it differs.
const defaultRequestedRadius = 16

// frontend implements core.Frontend for protocol 2193.
type frontend struct{}

func (frontend) Build() core.BuildInfo { return buildInfo() }

func (frontend) PacketNames() map[uint32]string { return packetNames }

// RunSession joins one session and plays it until it ends.
func (frontend) RunSession(ctx context.Context, s *core.Session) core.SessionEnd {
	cfg := s.Config()
	counters := &s.Fleet().C
	var network minecraft.Network = countingNetwork{
		log:     slog.New(sessionLog{s: s, transport: true}),
		rxBytes: &counters.RxBytes,
		txBytes: &counters.TxBytes,
	}
	addr := cfg.Addr
	if cfg.Transport == core.TransportNetherNet {
		nn, err := netherNet(s)
		if err != nil {
			return joinFailure(ctx, core.StageDial, err)
		}
		network, addr = nn, "http://"+cfg.Addr
	}
	memoryTier := 0
	if cfg.CSG {
		memoryTier = 4
	}
	dialer := minecraft.Dialer{
		ErrorLog: slog.New(sessionLog{s: s}),
		IdentityData: login.IdentityData{
			DisplayName: s.Bot.Name,
			Identity:    s.Bot.UUID,
			XUID:        s.Bot.XUID,
		},
		KeepXBLIdentityData: cfg.FakeXUID,
		ClientData: login.ClientData{
			CompatibleWithClientSideChunkGen: cfg.CSG,
			MaxViewDistance:                  core.MaxRadius,
			MemoryTier:                       memoryTier,
		},
		EnableClientCache:    true,
		DownloadResourcePack: func(uuid.UUID, string, int, int) bool { return false },
	}

	joinCtx, cancel := context.WithTimeout(ctx, cfg.SpawnTimeout)
	defer cancel()
	conn, err := dialer.DialContextNetwork(joinCtx, network, addr)
	if err != nil {
		return joinFailure(ctx, dialStage(err), err)
	}
	if err := conn.DoSpawnContext(joinCtx); err != nil {
		_ = conn.Close()
		return joinFailure(ctx, core.StageSpawn, err)
	}
	return newBotConn(s, conn).play(ctx)
}

// netherNet is a NetherNet network that offers through the server's HTTP signaling endpoint, with a self-signed
// identity as BDS accepts from any client.
func netherNet(s *core.Session) (minecraft.NetherNet, error) {
	key, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		return minecraft.NetherNet{}, err
	}
	identity, err := nethernet.GenerateServerIdentity(key, "self")
	if err != nil {
		return minecraft.NetherNet{}, err
	}
	return minecraft.NetherNet{
		Signaling: endpoint.NewClient(),
		Dialer:    nethernet.Dialer{Identity: identity},
		Log:       slog.New(sessionLog{s: s, transport: true}),
	}, nil
}

// dialStage tells a failure of the transport connection (dial) from one during the Minecraft login (login).
func dialStage(err error) string {
	var op *net.OpError
	if errors.As(err, &op) && (op.Net == "raknet" || op.Net == "nethernet") {
		return core.StageDial
	}
	return core.StageLogin
}

func joinFailure(ctx context.Context, stage string, err error) core.SessionEnd {
	if ctx.Err() != nil {
		return core.SessionEnd{Reason: core.EndStopped}
	}
	return core.SessionEnd{Reason: core.EndJoinFailed, Stage: stage, Err: err}
}

// botConn is one spawned session: the connection plus the state the packet handlers keep.
type botConn struct {
	s    *core.Session
	cfg  *core.Config
	conn *minecraft.Conn
	gd   minecraft.GameData
	done chan struct{} // closed when play returns

	handledTeleport atomic.Bool // acknowledge a server teleport in the next input
	dead            atomic.Bool
	respawnSent     atomic.Bool
	tick            uint64
	walker          *core.Walker
	probe           prober
}

func newBotConn(s *core.Session, conn *minecraft.Conn) *botConn {
	cfg := s.Config()
	return &botConn{
		s:      s,
		cfg:    cfg,
		conn:   conn,
		gd:     conn.GameData(),
		done:   make(chan struct{}),
		walker: core.NewWalker(cfg.Walk, s.Bot.Index),
	}
}

// write sends a packet and counts it. Errors surface through the read loop when the connection is gone.
func (b *botConn) write(pk packet.Packet) {
	if err := b.conn.WritePacket(pk); err == nil {
		b.s.TxPacket(pk.ID())
	}
}

func (b *botConn) play(ctx context.Context) core.SessionEnd {
	defer close(b.done)
	pos := b.gd.PlayerPosition
	radius := int32(b.conn.ChunkRadius())
	b.s.Spawned(float64(pos[0]), float64(pos[1]), float64(pos[2]), b.gd.Dimension, radius)

	if b.cfg.Radius != defaultRequestedRadius {
		b.requestRadius(int32(b.cfg.Radius))
	}
	if b.cfg.LoadScreen {
		b.write(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeStart})
		b.write(&packet.ServerBoundLoadingScreen{Type: packet.LoadingScreenTypeEnd})
	}
	_ = b.conn.Flush()

	readErr := make(chan error, 1)
	go func() { readErr <- b.readLoop() }()
	if b.s.Probing() {
		go b.probeLoop()
	}

	ticker := time.NewTicker(time.Second / core.TicksPerSecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			b.close(readErr)
			return core.SessionEnd{Reason: core.EndStopped}
		case <-b.s.Leave():
			b.close(readErr)
			return core.SessionEnd{Reason: core.EndLeft}
		case err := <-readErr:
			if ctx.Err() != nil {
				return core.SessionEnd{Reason: core.EndStopped}
			}
			var kick minecraft.DisconnectError
			if errors.As(err, &kick) {
				return core.SessionEnd{Reason: core.EndKicked, Err: kick}
			}
			return core.SessionEnd{Reason: core.EndDropped, Err: err}
		case c := <-b.s.Requests():
			if c.Op == core.OpRadius {
				b.requestRadius(int32(c.Radius))
				_ = b.conn.Flush()
			}
		case <-ticker.C:
			// A failed write means the connection is gone; the read loop reports why.
			_ = b.walkTick()
		}
	}
}

// close ends the connection on the bot's initiative and waits for the read loop.
func (b *botConn) close(readErr <-chan error) {
	_ = b.conn.Close()
	<-readErr
}

func (b *botConn) readLoop() error {
	for {
		pk, err := b.conn.ReadPacket()
		if err != nil {
			return err
		}
		b.s.RxPacket(pk.ID())
		b.handle(pk)
	}
}

func (b *botConn) requestRadius(r int32) {
	b.write(&packet.RequestChunkRadius{ChunkRadius: r, MaxChunkRadius: uint8(r)})
}

// walkTick sends one PlayerAuthInput. The bot walks its circle at constant height; a teleport from the server is
// acknowledged in the next input.
func (b *botConn) walkTick() error {
	b.tick++
	dx, dz := b.walker.Step()
	x, y, z := b.s.Advance(dx, dz)
	flags := protocol.NewInputFlags(packet.InputFlagCount)
	move := mgl32.Vec2{}
	if b.walker.Moving() {
		flags.Set(packet.InputFlagUp)
		move = mgl32.Vec2{0, 1}
	}
	flags.Set(packet.InputFlagVerticalCollision)
	if b.handledTeleport.Swap(false) {
		flags.Set(packet.InputFlagHandledTeleport)
	}
	yaw := float32(b.walker.YawDegrees())
	err := b.conn.WritePacket(&packet.PlayerAuthInput{
		Position:           mgl32.Vec3{float32(x), float32(y), float32(z)},
		MoveVector:         move,
		AnalogueMoveVector: move,
		RawMoveVector:      move,
		HeadYaw:            yaw,
		Yaw:                yaw,
		InteractYaw:        yaw,
		InputData:          flags,
		InputMode:          packet.InputModeMouse,
		PlayMode:           packet.PlayModeScreen,
		InteractionModel:   packet.InteractionModelCrosshair,
		Tick:               b.tick,
		Delta:              mgl32.Vec3{float32(dx), 0, float32(dz)},
	})
	if err == nil {
		b.s.TxPacket(packet.IDPlayerAuthInput)
	}
	return err
}
