// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"sync/atomic"

	"github.com/sandertv/go-raknet"
	"github.com/sandertv/gophertunnel/minecraft"
)

// countingNetwork is gophertunnel's RakNet network with the UDP socket wrapped, so rx and tx count every datagram
// payload byte, RakNet framing and resends included. The connection handed to gophertunnel is RakNet's own, so
// none of its optional transport methods are hidden.
type countingNetwork struct {
	log     *slog.Logger
	rxBytes *atomic.Int64
	txBytes *atomic.Int64
}

func (n countingNetwork) dialer() raknet.Dialer {
	return raknet.Dialer{ErrorLog: n.log, UpstreamDialer: countingDialer{rx: n.rxBytes, tx: n.txBytes}}
}

// DialContext dials a RakNet connection over a counted UDP socket.
func (n countingNetwork) DialContext(ctx context.Context, address string) (net.Conn, error) {
	return n.dialer().DialContext(ctx, address)
}

// PingContext sends an unconnected ping over a counted UDP socket.
func (n countingNetwork) PingContext(ctx context.Context, address string) ([]byte, error) {
	return n.dialer().PingContext(ctx, address)
}

// Listen is not supported; the bot only dials.
func (n countingNetwork) Listen(string) (minecraft.NetworkListener, error) {
	return nil, errors.New("legacy1001: listening is not supported")
}

type countingDialer struct {
	rx, tx *atomic.Int64
}

func (d countingDialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	conn, err := (&net.Dialer{}).DialContext(ctx, network, address)
	if err != nil {
		return nil, err
	}
	return &countingConn{Conn: conn, rx: d.rx, tx: d.tx}, nil
}

type countingConn struct {
	net.Conn
	rx, tx *atomic.Int64
}

func (c *countingConn) Read(b []byte) (int, error) {
	n, err := c.Conn.Read(b)
	c.rx.Add(int64(n))
	return n, err
}

func (c *countingConn) Write(b []byte) (int, error) {
	n, err := c.Conn.Write(b)
	c.tx.Add(int64(n))
	return n, err
}
