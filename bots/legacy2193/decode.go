// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"

	"github.com/EndstoneMC/bedrock-bench/bots/core"
	"github.com/sandertv/gophertunnel/minecraft/protocol/packet"
)

// packetNames maps every packet id of the protocol to its gophertunnel type name, e.g. 58 -> "LevelChunk".
// packetIDs is the inverse.
var packetNames, packetIDs = packetTables()

func packetTables() (map[uint32]string, map[string]uint32) {
	names := map[uint32]string{}
	for _, pool := range []packet.Pool{packet.NewServerPool(), packet.NewClientPool()} {
		for id, newPacket := range pool {
			names[id] = strings.TrimPrefix(fmt.Sprintf("%T", newPacket()), "*packet.")
		}
	}
	ids := make(map[string]uint32, len(names))
	for id, name := range names {
		ids[name] = id
	}
	return names, ids
}

// decodeMarkers are the phrases gophertunnel uses when a packet or a batch cannot be decoded. Other errors it
// logs, such as a failed socket read when a server goes away, are not decode errors: they must not turn a crashed
// server into an invalid run.
var decodeMarkers = []string{
	"decode packet",
	"read packet header",
	"decode batch",
	"decompress batch",
	"verify batch",
	"unexpected packet",
}

var (
	packetTypeRe = regexp.MustCompile(`decode packet \*packet\.(\w+)`)
	packetIDRe   = regexp.MustCompile(`unexpected packet \(ID=(\d+)\)`)
)

// isDecodeError reports whether a logged error message describes undecodable data.
func isDecodeError(msg string) bool {
	for _, m := range decodeMarkers {
		if strings.Contains(msg, m) {
			return true
		}
	}
	return false
}

// decodedPacketID extracts the id of the packet a decode error message names, or nil.
func decodedPacketID(msg string) *uint32 {
	if m := packetTypeRe.FindStringSubmatch(msg); m != nil {
		if id, ok := packetIDs[m[1]]; ok {
			return &id
		}
	}
	if m := packetIDRe.FindStringSubmatch(msg); m != nil {
		if v, err := strconv.ParseUint(m[1], 10, 32); err == nil {
			id := uint32(v)
			return &id
		}
	}
	return nil
}

// sessionLog is the slog handler behind gophertunnel's Dialer.ErrorLog. Decode errors become decode_error events
// and count against the run; everything else at warning level or above goes to stderr.
type sessionLog struct {
	s         *core.Session
	transport bool // the transport's log: diagnostics only, never decode errors
}

func (h sessionLog) Enabled(_ context.Context, level slog.Level) bool { return level >= slog.LevelWarn }

func (h sessionLog) Handle(_ context.Context, r slog.Record) error {
	if !h.transport && r.Level >= slog.LevelError && isDecodeError(r.Message) {
		h.s.DecodeError(decodedPacketID(r.Message), r.Message)
		return nil
	}
	src := "gophertunnel"
	if h.transport {
		src = h.s.Config().Transport
	}
	h.s.Logf("%s %s: %s", src, strings.ToLower(r.Level.String()), r.Message)
	return nil
}

func (h sessionLog) WithAttrs([]slog.Attr) slog.Handler { return h }

func (h sessionLog) WithGroup(string) slog.Handler { return h }
