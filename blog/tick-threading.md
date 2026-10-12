---
title: "Drawing a map of the Bedrock tick"
date: 2026-10-12
summary: "We mapped which thread does what on every Bedrock server tick, and the map explained why Endstone 0.11.14 keeps game logic on the main thread."
tags: [performance, threading, ticks, bds, endstone-0.11.14]
---

Every performance idea we had for Endstone 0.11.14 ran into the same question. Could this work happen on another thread?

On our benchmark machine the server gets the 16 hardware threads of the performance cores. With 100 players spread across a fresh world, vanilla Bedrock Dedicated Server 1.26.52 ran at 9.40 TPS and spent 105 ms of main thread time on each tick, against a budget of 50. The whole process used 1.07 CPU cores. Most of the server's share of the machine sat idle while it fell behind.

We could not answer the question. We did not know which thread ran which work, or what would break if we moved it. So we drew a map of the tick. We went through one tick piece by piece and wrote down which thread ran each part, and in what order.

## A loop that drops ticks

The server runs its game logic on one main thread, around a loop. Each pass works out how much time has gone by and how many ticks it owes.

We expected a lagging server to catch up quickly with a burst of ticks. It does not. A pass recovers at most about two ticks, and the rest of the lost time is gone. A server running at 10 TPS never gets those ticks back. Our benchmarks measure TPS as game time advanced per second of wall time, so the lost ticks show up in the number.

The map explained a bug in our own numbers too. The level tick is only part of each pass, and network work and other jobs run outside it. Endstone's MSPT had timed the level tick alone, so it read low on a busy server. Since 0.11.14 it covers the whole pass and no longer rounds to whole milliseconds.

## The order of a tick

Inside the level tick, much of the gameplay runs as a list of small systems. They run in a fixed order, one after another, on the main thread.

Per-chunk work, such as block entity updates, starts from two places. One is a system in that list. The other is ticking areas, such as those made with the /tickingarea command. Look at only one and you miss part of the cost.

Order turned out to matter less than we feared in one important place. Chunk ticks are already shuffled. Every 20 ticks the server reshuffles the order it ticks chunks in, using the world's own random generator. Ticking groups of chunks on several threads would change that order. Since vanilla never kept the order fixed, we would not count a new order by itself as a gameplay change.

## Other threads at work

Work that leaves the main thread goes to named worker pools. The names show up in any profiler, so you can watch them yourself. The size of the chunk generation pool depends on the machine and the max-threads setting, and the network pool always has one worker. On our machine that meant seven workers for chunk generation and one for the network. Mobs, redstone, random ticks, chunk publishing and the autosave all ran on the main thread.

The most useful part of the map was a list of signs that more than one thread touches something. We found four.

When chunk ticking finds a chunk's block entities locked by another thread, it skips them for that tick. Vanilla already does this, and those block entities silently lose a tick. We note it on every BDS update, and we would treat any change of ours that makes those skips more common as a gameplay change.

The main table of loaded chunks has no lock around its reads. Only one thread may add or remove chunks, and other threads can read it safely only while nothing is being added or removed. So a worker can never add a finished chunk to the table, or remove one, by itself. Any parallel design has to hand that job back to the main thread.

The object the server uses to read and write blocks keeps a small mutable cache and remembers which thread owns it, so two threads cannot share one.

Some kinds of objects, when released on any thread, go onto a list guarded by a lock and are destroyed later on the main thread. The server does this on purpose, so moving that cleanup to another thread would need an audit first.

Together, these made parallel ticking far riskier than it looked from the outside. Ticking dimensions or groups of chunks side by side would need a block-access object on every thread, and no worker could ever add or remove chunks from the main table. Parallel ticking is not part of 0.11.14.

## What we did instead

We kept game logic on the main thread and made it cheaper there. The autosave picker, random ticks, the locator bar, player lookups and chunk publishing all stayed put and got faster. The generation pool kept its size and started using all seven of its workers. The network thread stayed alone and got a faster compressor. Each of those changes has its own post, listed in the [series index](README.md).

With 100 spread players on Windows, Endstone 0.11.14 holds 20.0 TPS where vanilla BDS 1.26.52 manages 9.40. Main thread time per tick fell from 105 ms to 43 ms. The median player reaches 90% of its view in 81 seconds instead of 192. In steady play the process uses 1.04 cores, against 1.07 for vanilla. The server now runs more than twice as many ticks per second on about one core.

## What we took away

A new BDS build can add or drop a lock, or move a system to another place in the tick. So on every update we check the four signs again before we trust any change that touches the tick.
