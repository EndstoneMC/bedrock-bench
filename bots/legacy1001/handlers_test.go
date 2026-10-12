// SPDX-License-Identifier: Apache-2.0

package main

import (
	"testing"

	"github.com/sandertv/gophertunnel/minecraft/protocol"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

func TestSubChunkLimitModes(t *testing.T) {
	cases := []struct {
		name  string
		chunk packet.LevelChunk
		limit int32
		ok    bool
	}{
		{"limited", packet.LevelChunk{SubChunkCount: protocol.SubChunkRequestModeLimited, HighestSubChunk: 10}, 10, true},
		{"limitless", packet.LevelChunk{SubChunkCount: protocol.SubChunkRequestModeLimitless}, -1, true},
		{"sub-chunks inline", packet.LevelChunk{SubChunkCount: 8}, 0, false},
		{"empty column", packet.LevelChunk{}, 0, false},
	}
	for _, c := range cases {
		limit, ok := subChunkLimit(&c.chunk)
		if limit != c.limit || ok != c.ok {
			t.Errorf("%s: subChunkLimit = (%d, %v), want (%d, %v)", c.name, limit, ok, c.limit, c.ok)
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
	if n := len(subChunkOffsets(1, -1)); n != 8 {
		t.Errorf("a limitless nether column has %d offsets, want 8", n)
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
