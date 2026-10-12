---
title: "One Network Thread Is Plenty"
date: 2026-10-12
summary: "After login, one thread sends every packet a Bedrock server writes, and we kept it at one thread while cutting its load in the busiest minute of a 100-player join from 238 to 62 ms per second."
tags: [performance, networking, compression, profiling, endstone]
---

During a 100-player join on our 0.11 development build, one thread kept showing up near the top of the profile. It was busy 238 milliseconds out of every second. Next to it, the terrain generation workers had seven threads between them. This one was alone, and its name in the profiler made us think it handled web requests.

It turned out to be far more important than that. Once a player has logged in, that thread sends every packet the server writes to them, from chunks to chat messages. Before login, the main thread sends them itself.

The obvious fix was more network threads. We kept the thread count at one. This post is about why, and about what we changed instead.

## One worker, on every machine

The first question was how many network workers Bedrock Dedicated Server creates. The answer is one. It does not depend on the core count or on `max-threads` in `server.properties`. A server on a 64-core machine sends its packets on one thread, the same as a laptop.

Lowering the count was never an option. Setting it to zero removes the thread entirely, and parts of the server assume it exists. Any change would have to go up.

## Everything else on that thread

Before raising the count, we listed every job that runs on that thread. The list was longer than we expected:

- sending each connection's packets
- telemetry
- outgoing web requests
- server discovery
- NetherNet and Xbox Live signaling
- the background checks that run while a player joins
- DNS lookups
- the HTTP and WebSocket modules available to scripts
- NetherNet's built-in HTTP signaling server

Every one of these has only ever run on a single thread. At least one keeps shared bookkeeping without a lock, which is fine today because nothing else can touch it at the same moment. A second worker would break that assumption. Raising the count would be a threading change for everything on the list, and most of it has nothing to do with performance.

The packet path itself was ready for more workers. Each connection's outgoing batches sit in a queue that only one task drains at a time, so packets stay in order whatever the worker count. Encryption state belongs to a single connection and only its own send path touches it. The transports underneath accept sends from several threads. The risk was in the other eight jobs.

## We had been here before

Earlier this year we built a version that gave every connection its own lane on a thread pool (#412). It moved compression and encryption off the main thread. The main thread got no faster, because decoding packets and running their handlers still had to happen there, and passing data between threads added work of its own. Incoming packets also surfaced about two ticks later, which hurt most during the join handshake. At 100 players it came out about 15 ms per second worse than doing nothing.

So this time we started with a plainer question. Is the thread actually short of time?

## How busy is busy

Our harness samples every thread's CPU time in 5-second buckets, which gives each thread a timeline in milliseconds per second.

We learned to scale by TPS before comparing runs, because traffic is generated per tick. Vanilla's steady 13 ms/s looked like less than a third of our 46 until we remembered vanilla was running at 9.2 TPS. At 20 TPS it would be about 28 ms/s.

We also took about 70 percent as the thread's real ceiling, because a single worker serving a queue gets slow long before it is 100 percent busy.

Measured that way, the thread was nowhere near its limit. Before any change, steady play with 100 players at 20 TPS kept it busy 46 ms per second, or 4.6 percent. The first minute of the join averaged 24 percent. A second thread had nothing to fix.

That did not make the 238 ms free. During a join, every player's chunks wait in line on that one thread, so each millisecond spent on one batch delays the next. We wanted to know where the time went.

## Making the work cheaper

Encryption was a rounding error on Windows, about 1 percent of the thread's time. On Linux the server's crypto runs without CPU-specific fast paths, so it costs noticeably more there. On Windows, our stack sampler kept finding packet compression with zlib on the thread's stacks.

We already had a faster compressor. Our development branch had swapped zlib for libdeflate on that path in August, and the 0.11 line did not have it yet. Both produce the same deflate format, so the client cannot tell the difference, and our tests check both directions against real zlib. On 39.7 MB of recorded gameplay traffic, split into 3,465 batches, libdeflate at level 6 was 4.6 times faster than zlib set up the way vanilla uses it, for output 0.40 percent larger.

On a synthetic corpus, libdeflate was only 1.85 times faster. Real Minecraft batches are repetitive enough that the gap more than doubles on live traffic.

Two smaller changes went in alongside it. We rebuilt Endstone's own per-packet handling on the send path, and we dropped a per-packet debugging step whose output a server never reads.

Our first build had an ugly side effect. Each connection kept a compression buffer sized for the largest batch it had ever sent, and the login batch is several megabytes before compression. That came to about 6.5 MB per player, or 650 MB at 100 players, all of it arriving with the join burst. Commit fe86ade3f moved those buffers from each connection to each thread, and steady private memory went from 5.49 GB to 4.85 GB.

## The result

Same harness and seed, with 100 bots spread across a fresh world, before and after the three network changes together:

| Network thread              |   Before |   After |
|-----------------------------|---------:|--------:|
| First minute of the join    | 238 ms/s | 62 ms/s |
| Whole join phase            |  99 ms/s | 45 ms/s |
| Steady play                 |  46 ms/s | 40 ms/s |

It is still one thread. During the busiest minute of the join it now sits idle 94 percent of the time. The changes shipped in Endstone 0.11.14 as improved packet compression performance. Gameplay and the wire format are unchanged, and the client decodes the stream exactly as it always has.

## What we took away

We now check how close a thread is to its ceiling, and what else runs on it, before we reach for more workers. Here the answer was 238 ms per second at the peak of the join, on a thread shared by nine jobs. The network changes cut that to 62, 3.8 times less, and the whole join phase from 99 to 45 ms per second. In that busiest minute, every joining player's packets now queue behind about a quarter of the work, and nothing else on the thread had to change.
