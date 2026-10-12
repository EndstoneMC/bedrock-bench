// SPDX-License-Identifier: Apache-2.0

package main

import (
	"bytes"
	"encoding/binary"
	"testing"

	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// TestSubChunkHeightMapWireLayout builds a sub-chunk entry byte by byte in the protocol 2193 layout, where each
// height map is 16 rows of a varuint32 length (16) and 16 signed heights, 272 bytes in all, and decodes it with
// upstream gophertunnel. This is why bots/PATCHES.md carries no sub-chunk patch: the legacy fork read the same
// 272 bytes as one flat block.
func TestSubChunkHeightMapWireLayout(t *testing.T) {
	var wire bytes.Buffer
	wire.Write([]byte{0x00, 0xfc, 0x00})             // offset (0, -4, 0)
	wire.WriteByte(protocol.SubChunkResultSuccess)   // result
	wire.Write([]byte{0x01, 0x03, 0xaa, 0xbb, 0xcc}) // raw payload: present, length 3
	wire.WriteByte(protocol.HeightMapDataHasData)
	wire.WriteByte(0x01) // height map present
	for z := 0; z < 16; z++ {
		wire.WriteByte(16)
		for x := 0; x < 16; x++ {
			wire.WriteByte(byte(int8(z*16 + x - 128)))
		}
	}
	wire.WriteByte(protocol.HeightMapDataTooHigh)
	wire.WriteByte(0x00) // no render height map
	wire.WriteByte(0x01) // blob hash present
	_ = binary.Write(&wire, binary.LittleEndian, uint64(0x1122334455667788))
	if got := wire.Len(); got != 3+1+5+1+1+272+1+1+1+8 {
		t.Fatalf("test data is %d bytes", got)
	}

	var entry protocol.SubChunkEntry
	r := bytes.NewReader(wire.Bytes())
	entry.Marshal(protocol.NewReader(r, 0, false))
	if r.Len() != 0 {
		t.Fatalf("%d bytes left unread", r.Len())
	}
	hm, ok := entry.HeightMapData.Value()
	if !ok {
		t.Fatal("height map missing")
	}
	if hm[0][0] != -128 || hm[15][15] != 127 || hm[3][5] != int8(3*16+5-128) {
		t.Errorf("heights decoded wrongly: [0][0]=%d [15][15]=%d", hm[0][0], hm[15][15])
	}
	if _, ok := entry.RenderHeightMapData.Value(); ok {
		t.Error("render height map should be absent")
	}
	if h, ok := entry.BlobHash.Value(); !ok || h != 0x1122334455667788 {
		t.Errorf("blob hash = %x, %v", h, ok)
	}
}

func TestPacketTables(t *testing.T) {
	cases := map[uint32]string{
		packet.IDLevelChunk:                  "LevelChunk",
		packet.IDSubChunk:                    "SubChunk",
		packet.IDPlayerAuthInput:             "PlayerAuthInput",
		packet.IDCorrectPlayerMovePrediction: "CorrectPlayerMovePrediction",
		packet.IDMoveActorDelta:              "MoveActorDelta",
	}
	for id, name := range cases {
		if packetNames[id] != name {
			t.Errorf("packetNames[%d] = %q, want %q", id, packetNames[id], name)
		}
		if packetIDs[name] != id {
			t.Errorf("packetIDs[%q] = %d, want %d", name, packetIDs[name], id)
		}
	}
}

func TestDecodeErrorClassification(t *testing.T) {
	sub := uint32(packet.IDSubChunk)
	cases := []struct {
		msg    string
		decode bool
		id     *uint32
	}{
		{"read packet: decode packet *packet.SubChunk: unexpected EOF", true, &sub},
		{"decode packet *packet.SubChunk: 3 unread bytes left: 0x010203", true, &sub},
		{"read packet: decode packet *packet.NoSuchThing: EOF", true, nil},
		{"decompress batch: decompress flate: unexpected EOF", true, nil},
		{"decode batch: packet length 900 exceeds remaining 12", true, nil},
		{"receive: read packet header: EOF", true, nil},
		{"unexpected packet (ID=999)", true, func() *uint32 { v := uint32(999); return &v }()},
		{"read batch: read udp 127.0.0.1:1->127.0.0.1:2: connection reset", false, nil},
		{"handle packet: connection refused", false, nil},
	}
	for _, c := range cases {
		if got := isDecodeError(c.msg); got != c.decode {
			t.Errorf("isDecodeError(%q) = %v", c.msg, got)
		}
		got := decodedPacketID(c.msg)
		switch {
		case c.id == nil && got != nil:
			t.Errorf("decodedPacketID(%q) = %d, want nil", c.msg, *got)
		case c.id != nil && (got == nil || *got != *c.id):
			t.Errorf("decodedPacketID(%q) = %v, want %d", c.msg, got, *c.id)
		}
	}
}

func TestSubChunkOffsetsMapToProtocol(t *testing.T) {
	got := subChunkOffsets(0, 3)
	want := []protocol.SubChunkOffset{{0, -4, 0}, {0, -3, 0}, {0, -2, 0}}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("offset %d = %v, want %v", i, got[i], want[i])
		}
	}
}
