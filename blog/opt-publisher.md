---
title: "Parking the chunks that are not ready"
date: 2026-10-12
summary: "Players waiting for new terrain made the server re-check thousands of unfinished chunks on every tick, and parking those chunks together with a fix to a contended lock took about 11 ms off each tick with 100 players."
tags: [performance, chunks, chunk-sending, bds, endstone-0.11.14]
---

In [an earlier post](chunk-storage.md) we fixed the autosave picker, which had been taking about 45% of the main thread. With that fix loaded into vanilla 1.26.51 as a prototype patch, 60 spread-out players held 20 TPS. The steady state looked healthy. The join did not. While players waited for their terrain, one part of the main thread took 41% of its time, about 9.6 ms per tick. That part was chunk publishing, the code that sends finished chunks to each player.

Our target load was 100 players spread across a fresh world, view distance 10, simulation distance 4, client-side chunk generation on. Vanilla managed 10.75 TPS there, with 93 ms of main thread per tick.

## What the publisher does on every tick

Every player has a queue of chunks waiting to be sent. On each tick the server sorts that queue by distance from the player and walks it. Ready chunks go out. Chunks that are still generating stay in the queue and get asked about again on the next tick.

A player who has just joined or teleported has a queue made almost entirely of unfinished chunks. With 100 such players, the server walked 100 queues of mostly unready chunks 20 times a second and got the same answer each time.

## Waiting on memory

A sampling profiler showed where the time went. More than 90% of the publishing step's own time went to a single read: the value in each chunk that says whether it is ready.

That pattern means the processor is waiting for memory. The cost is pulling hundreds of chunk objects into cache for each player on every tick, only to read one value from each. Whatever we built had to stop looking at those chunks at all.

Before settling on a design we recounted the publishing cost in a fresh process, and it did not match our comparison table. Our analysis script had been sharing a cache of call stacks across profiles, so one profile could borrow another's stacks. We fixed the cache and stopped trusting any table built before the fix.

## Feeding the loop less

The loop that walks the queue is buried deep in the server's per-player update. Replacing it would have meant reimplementing a large part of that update and maintaining it through every BDS release.

So we left the loop alone and changed what it sees. Our code runs each tick just before the loop. It moves every queued chunk that is outside the player's view or not finished yet out of the queue and into a side list. We call this parking. The vanilla loop then runs unchanged on a queue that holds only chunks it can send.

A parked chunk usually comes back through the server's own chunk-loaded notification, which fires once the chunk is ready. Parked chunks are also rechecked when the player's view moves or changes size. Every 100 ticks a slow sweep rechecks everything as a backstop.

If some path could make a chunk sendable without announcing it, that chunk would sit parked until the next sweep. So we checked every way a queued chunk can become ready to send and confirmed that each one is covered. Spotting a moved view was simpler. Comparing the view's bounds before and after each update catches every change.

## One lock, a hundred times a tick

Parking was half of the change. The other half was a lock. The main thread was spinning on a lock it shared with the chunk generation workers. Each player's tick rebuilt the list that decides which chunk to generate next, using the positions of every player. With 100 players that came to 100 full rebuilds per tick, and each one waited for the workers to let go of the list.

We now rebuild the list once, after every player has ticked. Only the moment of the rebuild changes, so this part is timing-only.

## Matching the server's hash

Parking edits vanilla's queue in place, so our lookups have to hash chunk positions exactly as the server does. The Windows and Linux servers hash chunk positions differently. We matched the server's hashing on each platform and tested it against the server's own behaviour on both.

As a guard, Endstone first checks that its own lookup finds each of the first 256 queue entries it sees. If one is missing, it logs a warning and leaves the publisher to run as vanilla does.

Before anything ran in a live server, a small test program drove the parking logic tick by tick against fake players and fake chunks.

## What it did

We first built the change as a prototype patch loaded into vanilla 1.26.51, with the same 100 spread players. Parking and the single rebuild together took about 11 ms off every tick on the main thread. Together with three other prototypes, for chunk blending, player lookups and the locator bar, the server went from 10.75 TPS at 93 ms per tick to between 19.0 and 19.7 TPS at about 50 ms.

Endstone had moved to BDS 1.26.52 by the time the change shipped, and it carried over with little work. It is in Endstone 0.11.14 as commit 099d6f028.

On the release benchmark, with every 0.11.14 change in place, the median time for a player's view to load on Windows fell from 192 s on vanilla 1.26.52 to 81 s. The other 0.11.14 changes contribute to that figure as well.

## Checking it against vanilla

An instrumented build ran vanilla's own sending decision next to ours throughout a chaos run, our stress test of randomized joins, kicks, teleports, deaths and dimension changes against 100 bots. On Windows it checked parked chunks 424.7 million times and found none that vanilla would have sent. On Linux it made 679.9 million checks, also with none.

Send order showed one real difference. On Linux, 149,125 of 371,264 checked sends went out in a different order from vanilla. All of them were among chunks at exactly the same distance from the player. Vanilla's sort makes no promise about ties, so a shorter queue can put two equally distant chunks the other way round. Outside of ties the order matched every time.

## Looking back

The profile told us the most. When nearly all of a function's time sits on one memory read, the function is touching too many objects, and here that meant hundreds of chunks per player on every tick. Parking keeps those chunks out of the loop until they have something to send. We trusted the chunk-loaded notification to bring them back only after checking every way a chunk becomes ready, and we kept the slow sweep anyway.
