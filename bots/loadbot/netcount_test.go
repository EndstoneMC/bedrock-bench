// SPDX-License-Identifier: Apache-2.0

package main

import (
	"context"
	"net"
	"sync/atomic"
	"testing"
	"time"
)

func TestCountingDialerCountsDatagramBytes(t *testing.T) {
	server, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	go func() {
		buf := make([]byte, 2048)
		n, addr, err := server.ReadFrom(buf)
		if err != nil {
			return
		}
		_, _ = server.WriteTo(append(buf[:n:n], buf[:n]...), addr) // echo twice the payload
	}()

	var rx, tx atomic.Int64
	conn, err := countingDialer{rx: &rx, tx: &tx}.DialContext(context.Background(), "udp", server.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 64)
	n, err := conn.Read(buf)
	if err != nil {
		t.Fatal(err)
	}
	if n != 10 || tx.Load() != 5 || rx.Load() != 10 {
		t.Errorf("read %d bytes; counted tx=%d rx=%d, want 5 and 10", n, tx.Load(), rx.Load())
	}
}

func TestProberMatchesScaledEchoes(t *testing.T) {
	var p prober
	start := time.Now()
	if _, lost := p.next(start); lost {
		t.Error("first probe reported a loss")
	}
	stamp, lost := p.next(start.Add(time.Second))
	if !lost {
		t.Error("unanswered probe not reported lost")
	}
	for _, echo := range []int64{stamp, stamp * 1000} {
		p.mu.Lock()
		p.pending = true
		p.mu.Unlock()
		rtt, ok := p.take(echo, start.Add(1500*time.Millisecond))
		if !ok || rtt != 500*time.Millisecond {
			t.Errorf("echo %d of stamp %d: rtt %v, matched %v", echo, stamp, rtt, ok)
		}
	}
	p.mu.Lock()
	p.pending, p.stamp = true, 5_000_000
	p.mu.Unlock()
	if _, ok := p.take(5000, time.Now()); !ok {
		t.Error("echo divided by 1000 not matched")
	}
	if _, ok := p.take(5000, time.Now()); ok {
		t.Error("answered probe matched twice")
	}
	p.mu.Lock()
	p.pending = true
	p.mu.Unlock()
	if _, ok := p.take(42, time.Now()); ok {
		t.Error("unrelated timestamp matched")
	}
}
