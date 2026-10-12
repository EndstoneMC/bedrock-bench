---
title: "Choosing which chunk to build next"
date: 2026-10-12
summary: "With 100 spread-out players, Bedrock's chunk generation spent more time deciding what to build than building it, and the change that sped up loading turned out to be in a different place."
tags: [performance, chunk-generation, worldgen, endstone, profiling]
---

When we started load-testing with 100 players spread across a fresh world, terrain arrived slowly. Players teleported to their spots, and some waited several minutes for the ground under their feet. The generation workers looked busy the whole time, so our first guess was that worldgen is simply expensive.

The profile said otherwise.

## Reading the profile

We pointed a sampling profiler at the generation workers of a BDS 1.26.51 server during a 100-player load, with only a few of our test shims loaded. Real terrain work, such as noise, caves and ores, used less than half of the pool's CPU. The other 54% went to one job: deciding which chunk to generate next.

Whenever too few generation tasks are in flight, the server tops up the work queue. It scores every chunk waiting to be generated against every player and ticking area, sorts the list, and launches the best few dozen. A chunk's score is its distance to the nearest player, with a bonus for chunks the player is facing. At 100 players, every pass scores millions of chunk and player pairs.

We had no counters in place, but the profile held a clue. Sorting time grows in a known way with the length of the list, so the 0.55 ms each pass spent sorting told us how long the queue was: about 25,000 waiting chunks. Scoring took about 50 ms per pass. Spread over 25,000 chunks and 100 players, that is about 20 ns for every chunk and player pair.

Each pass launched 29 to 42 tasks. Only one thread can run a pass at a time, and tasks finished faster than a pass took, so a pass was always in flight, hopping from worker to worker. That capped the pool at 500 to 730 tasks per second. Of 1.6 busy cores, about 0.7 did actual terrain generation.

The main server thread paid too. Every player's tick rebuilt the scoring list from all players, and each rebuild waited on a lock the workers were taking 25,000 times per pass. That waiting cost the main thread about 2 ms per tick.

In the steady state, after every view had loaded, scoring cost only 2 ms per second. Any gain would show up only while views load, such as after a join or a teleport.

## Is the order safe to change?

Before touching a scheduler, we wanted to know whether generation order can change the world, which would make any scheduling change a gameplay change.

As far as we could check, it cannot. Decoration reseeds its random generator for every chunk from the world seed and the chunk's coordinates, so a tree lands in the same place whichever thread gets there first. When two neighbouring chunks decorate near their shared border, a fixed rule based on their coordinates decides which one goes first. Structures that read blocks from a neighbouring chunk are the part we checked least.

Vanilla's order also varies already. Ties in the score are common, and the Windows and Linux standard libraries sort ties differently. Thread timing reshuffles the rest. A change in tie order stays within the variation vanilla already has. We still aimed for no reordering at all.

## The fix we designed

Our plan had two parts. The first was to rebuild the scoring list once after all players have ticked, instead of once per player per tick. At 100 players that removes 99 rebuilds per tick. It shipped in 0.11.14 in the same commit as the chunk-sending fixes (`099d6f028`).

The second was an exact, lock-free copy of the scoring. The player list is only rewritten on the main thread, so we could copy it after each rewrite and let workers score against the copy without the lock. For each chunk, a cheap lower bound rules out every player who cannot beat the current best, and the full formula runs only for the 3 to 6 players who can.

We dropped the idea of a spatial grid. The facing bonus lets a distant player looking at a chunk beat a nearer player looking away, so a grid would have to search a wide radius anyway.

Matching vanilla bit for bit took care. We compiled the file without fused multiply-add or fast-math and kept vanilla's order of additions. We also kept a vanilla overflow: the distance math wraps when a chunk and a player are roughly 700,000 blocks apart, and a faithful copy has to wrap the same way.

On paper this took a pass from 50 ms to about 3 ms and promised 8 to 9 times more generation while views load.

## The wrong turn

First we measured what the copy would add on top of the rebuild change.

We sampled the stack of every generation worker about 35 times a second through a full 100-player join. This ran on a 1.26.52 Endstone development build that also carried the chunk-sending fixes. Scoring was down to about 9% of the busy workers' time. The two profiles came from different versions and builds, so we cannot credit the whole drop to the rebuild.

The surprise was in the per-worker view. Workers 0 and 1 were busy 95% of the time. Worker 2 was busy about half the time, and worker 3 only during the first 30 seconds. Workers 4, 5 and 6 never ran a single task. Busy workers spent about 1% of their time waiting on locks.

The cause was the pool's wake rule. When a task is queued, the pool wakes another worker only if the queue already holds a sizeable backlog for every worker that is awake. With short generation tasks, that means dozens of queued tasks per awake worker. The chunk scheduler launches only about 40 tasks at a time. So two or three workers did the work while the rest slept.

## Waking the pool

We set the pool's backlog requirement to zero, so any queued task can wake an idle worker (`cd97eea16`). That changes only when chunks get generated, which makes it timing-only. All seven workers now share the load.

In A/B runs with 100 spread players on Windows, lowering the requirement to a small non-zero value cut the median view load from 132 s to 86 s. The release ships it at zero.

With the other 0.11.14 changes in place, the release measured a median view load of 81 s on Windows, against 189 s for 0.11.13 and 192 s for vanilla BDS 1.26.52. On Linux it measured 130 s, against 192 s for 0.11.13 and 194 s for vanilla. When all 100 players join at once, TPS still dips for about 30 seconds, with a lowest 15-second average of 17.5 on Windows and 8.0 on Linux.

The exact scoring copy is still on the shelf. Once the idle workers woke up, it was no longer the first thing to fix.

## What we took away

The sort time told us how long the queue was without adding a single counter, and the cost model built on it explained the 50 ms passes. It still missed that most of the workers were asleep, because it added their CPU time together. We now sample every thread separately before we design a fix.
