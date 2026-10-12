// SPDX-License-Identifier: Apache-2.0

package main

import (
	"errors"
	"fmt"
	"net"
	"testing"

	"github.com/sandertv/gophertunnel/minecraft"
)

func TestKickMessage(t *testing.T) {
	addr := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 19672}
	opErr := func(op string, err error) error {
		return &net.OpError{Op: op, Net: "minecraft", Source: addr, Addr: addr, Err: err}
	}
	cases := []struct {
		name string
		err  error
		msg  string
		kick bool
	}{
		// What ReadPacket returns in v1.57.1 after the server sent a disconnect message.
		{"server disconnect", opErr("read packet", opErr("bench over", net.ErrClosed)), "bench over", true},
		{"disconnect error", fmt.Errorf("receive: %w", minecraft.DisconnectError("bench over")), "bench over", true},
		{"closed by the bot", opErr("read packet", net.ErrClosed), "", false},
		{"transport failure", opErr("read packet", &net.OpError{Op: "read", Net: "raknet", Err: net.ErrClosed}), "", false},
		{"plain error", errors.New("connection reset"), "", false},
	}
	for _, c := range cases {
		msg, kick := kickMessage(c.err)
		if msg != c.msg || kick != c.kick {
			t.Errorf("%s: kickMessage = (%q, %v), want (%q, %v)", c.name, msg, kick, c.msg, c.kick)
		}
	}
}
