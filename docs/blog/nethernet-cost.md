---
title: "Where NetherNet will cost us"
date: 2026-10-12
summary: "Before NetherNet becomes the only way onto a Bedrock server, we went looking for where its per-player cost will come from and found a Windows wait loop whose cost, on paper, grows with the square of the player count."
tags: [nethernet, networking, performance, windows]
---

Every benchmark run against BDS 1.26.52 opens with the same error in the log. It says NetherNet is the only supported transport type in this release and that players will not be able to connect without it. Nothing enforces it yet, and our load bots still join over RakNet. Sooner or later every server will be on NetherNet, though, so we wanted to know where its per-player cost will land before that happens.

NetherNet is Bedrock's WebRTC transport, from the same family of protocols a browser uses for a video call. A client finds the server through a signaling step, opens a path through NAT with ICE and STUN, and then sends game packets over an encrypted data channel on UDP. The data channel's timers run as tasks on the same network thread that services the sockets, so most of what follows happens on that one thread. That thread is the subject of [One network thread is plenty](opt-network-thread.md).

Our first guess was the encryption, which we have not measured yet. The first large cost we found was in the code that waits for traffic, which nobody profiles because it is supposed to be idle.

## The loop that waits

On Windows, every time the network thread wakes up, it re-arms every socket with a system call. When a socket was the reason for the wake, it then asks every socket what happened, with a second system call each. Linux uses epoll there, which reports only the sockets that are ready, so this part is a Windows problem.

The surprise came when we counted sockets. With two players connected, the server held four session sockets, one per player on each network interface. One wake therefore costs time in proportion to the number of players, and the number of wakes also grows with the number of players. The total grows with the square.

Wakes are frequent. The thread reads one datagram per wake, so every incoming datagram pays for a full pass over every socket. Every task handed to the thread wakes it as well, even a timer the thread set for itself.

## Five minutes with a stopwatch

To size the loop, we wrote a small Python script that makes the same two Windows calls on real sockets through ctypes, with an empty call subtracted as a baseline. Re-arming came to about 1.1 µs per socket. Asking for the status came to about 0.9 µs. Both stayed flat as we added sockets.

That gave us a model. A Bedrock client sends its input 20 times a second, so 100 players produce at least 2,000 inbound datagrams a second. If each one triggers a pass over one socket per player at 2 µs a socket, the loop spends 0.4 seconds of CPU every second before it reads a single packet. At 50 players the same sum gives 0.1 seconds.

That is a floor. Acknowledgements, other inbound traffic and the thread's own timers add wakes, and a second network interface doubles the sockets. At 200 players the sum reaches 1.6 seconds of CPU per second, which one thread cannot spend. Before that point the thread saturates, finds several sockets ready on each pass and serves them together, and that caps the cost. Below saturation it grows with the square of the player count.

## A wrong turn in the update loop

Next we followed the per-frame work. The server updates every connection on every pass of its network loop. On NetherNet, the update looks up the session's state each time, with locks and a map lookup per connection per frame. One of those locks is shared with the thread that creates new sessions.

We were ready to file this as NetherNet overhead. Then we looked at the same update on the RakNet side. It copies a block of statistics for every connection on every frame too. Both transports pay a per-connection toll, and from then on we compared against the other transport before blaming one.

Two more corrections followed. Per-frame cost is highest on an idle server and shrinks under load, because a loaded server runs fewer frames, so we priced it at the loaded rate. The lock is the bigger risk. A frame could stall for as long as a join holds it. We have not seen that happen, so it goes on the list for the load test.

## The cost of saying hello

Joins have their own bill. As far as we can tell, each new NetherNet connection generates a fresh key and certificate. That puts asymmetric crypto on a worker thread for every join, which we expect to cost milliseconds. We have not timed it.

Bedrock's own login encryption is switched off on NetherNet, because the data channel already encrypts. The login step still does its key exchange before it checks whether encryption is even on, so the expensive part runs anyway.

## One port for everyone

The obvious fix for the socket loop is to put every session on one shared socket. Bedrock's networking code can work that way, although no server setting turns it on. It also fits our goal for NetherNet in Endstone, which is what RakNet already gives admins: one port number and nothing to configure.

We had already learned what a single port does without sharing. Endstone 0.11.11 defaulted `server-udp-ports` to the server port. With one port in the window and one socket per session, a second player's session had no port left to bind. In a Docker test with two real clients, the second one sat on "Connecting" until the first one left. We removed that default in 0.11.12.

A shared socket should be much cheaper, since the loop then re-arms one socket per network interface instead of one per player. The routing still scales with the number of sessions. Each incoming game datagram is offered to every session in turn to find its owner. Each ICE connectivity check goes to all of them, and every session parses it in full. We have not profiled that scan.

That broadcast held one more surprise. A session that receives a check meant for someone else answers "unauthorized". The stray reply reaches the client ahead of the real answer, and the client drops its pending check. Sessions are asked in the order they were created, so only the oldest session on the socket is safe from this.

On a development branch we turned the shared socket on and suppressed the stray reply. With both changes in place, two real clients joined on one port, and the session sockets went from four to two, one per network interface. We only tested the two changes together, and neither has shipped yet.

## What comes next

Our published benchmarks still run over RakNet. Next comes a NetherNet load test on Windows, and the first number we want from it is the wait loop at 100 players, where our model says at least 0.4 seconds of CPU per second.
