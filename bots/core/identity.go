// SPDX-License-Identifier: Apache-2.0

package core

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
)

// UUID is an RFC 9562 UUID.
type UUID [16]byte

// NamespaceURL is the RFC 9562 name space for URLs (6ba7b811-9dad-11d1-80b4-00c04fd430c8).
var NamespaceURL = UUID{0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8}

// FakeXUIDBase is added to the bot index to form the XUID sent with -fake-xuid.
const FakeXUIDBase = 2535400000000000

// UUIDv5 returns the name-based (SHA-1) UUID of name in namespace ns.
func UUIDv5(ns UUID, name string) UUID {
	h := sha1.New()
	h.Write(ns[:])
	h.Write([]byte(name))
	var u UUID
	copy(u[:], h.Sum(nil))
	u[6] = u[6]&0x0f | 0x50
	u[8] = u[8]&0x3f | 0x80
	return u
}

// ParseUUID parses the canonical 8-4-4-4-12 form.
func ParseUUID(s string) (UUID, error) {
	var u UUID
	if len(s) != 36 || s[8] != '-' || s[13] != '-' || s[18] != '-' || s[23] != '-' {
		return u, fmt.Errorf("not a UUID: %q", s)
	}
	b, err := hex.DecodeString(strings.ReplaceAll(s, "-", ""))
	if err != nil {
		return u, fmt.Errorf("not a UUID: %q", s)
	}
	copy(u[:], b)
	return u, nil
}

// String formats u in the canonical lowercase form.
func (u UUID) String() string {
	var b [36]byte
	hex.Encode(b[0:8], u[0:4])
	b[8] = '-'
	hex.Encode(b[9:13], u[4:6])
	b[13] = '-'
	hex.Encode(b[14:18], u[6:8])
	b[18] = '-'
	hex.Encode(b[19:23], u[8:10])
	b[23] = '-'
	hex.Encode(b[24:], u[10:])
	return string(b[:])
}

// Namespace turns -identity-seed into a UUID name space: the seed itself when it is a UUID, otherwise
// UUIDv5(NamespaceURL, seed).
func Namespace(seed string) UUID {
	if u, err := ParseUUID(seed); err == nil {
		return u
	}
	return UUIDv5(NamespaceURL, seed)
}

// BotUUID is the identity of a bot: UUIDv5(Namespace(seed), name). It is stable across runs, so servers that key
// player data by UUID see the same players every time.
func BotUUID(seed, name string) string {
	return UUIDv5(Namespace(seed), name).String()
}

// FakeXUID is the XUID sent for the bot with the given index when -fake-xuid is set.
func FakeXUID(index int) string {
	return strconv.FormatInt(FakeXUIDBase+int64(index), 10)
}
