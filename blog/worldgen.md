---
title: "Ten ways to speed up terrain, and why we shipped none"
date: 2026-10-12
summary: "We profiled Bedrock's overworld generator during a 60-player join and drew up ten speedups that keep terrain identical, then found most of the generation workers asleep and shipped a different fix."
tags: [performance, worldgen, chunk-generation, profiling, endstone-0.11.14]
---

When a crowd joins a fresh Bedrock world, everyone waits on terrain. With 100 players, the wait for most of a player's view runs to minutes. We wanted to know whether the generator itself could get faster.

We profiled vanilla BDS 1.26.51 while 60 players joined a fresh world at once, each in their own part of the map. The server's seven chunk generation workers burned about 2.7 CPU cores between them over the 90-second join. We took that as the cost of expensive terrain. We did not ask why the number sat so far below seven.

We set one rule before touching anything. Any change had to produce the same world, block for block, from the same seed.

## The engine that wasn't there

We went in with a plan borrowed from Java Edition. Since 1.18.2, Java builds its terrain from a configurable tree of small math functions. Some Java mods compile that tree into tighter code. C2ME announced its compiler at up to 30% faster on datapack worlds.

We planned around that tree before checking whether the dedicated server had one. The Windows build we profiled has none. BDS builds its overworld the way Java did in 1.18.0, with the terrain math written directly in code. There was nothing to compile.

## Where the time went

A sampling profiler on the generation workers collected close to a million samples through the join. The first finding had nothing to do with terrain. 22.6% of the workers' time went to deciding which chunk to generate next. That one got its own post, [Choosing which chunk to build next](opt-genscheduler.md).

The largest pieces of the rest, as a share of all worker time:

| Work | Share |
|---|---:|
| Noise | 18.5% |
| Underground water, including the surface checks it triggers | 13% |
| Decoration, mostly ores and settling liquids | 9% |
| Biome placement | 6% |
| Filling in each block of the chunk | 6% |
| Blending structures into the terrain | 5.3% |
| Planning structure layouts | 5.2% |
| Old-style cave and canyon carvers | 4.5% |

The rows overlap, so they do not add up. The surface checks in the water row spend their time in noise, and the noise row counts that time too. In a short, lightly loaded run, noise alone rose to 30%.

## A cache that forgets

The biggest single opportunity was in underground water. To decide whether a spot below ground holds water, lava or air, the generator looks up the rough surface height at 13 places around it. Twelve of them land in neighbouring chunks. A small cache holds those heights for the chunk being built. Anything outside it is computed from scratch through several layers of noise, used once and thrown away. The cache never stores a miss.

Those recomputations cost 6.1% of worker time during the join and 9.5% in the lightly loaded run. The height at a spot depends only on the seed, the position and old terrain that does not change while the server runs, so it is safe to keep. A cache shared across chunks could serve the repeats between neighbours. We never measured how often it would hit.

Right next to it sat a smaller oddity. A built-in profiling hook takes a global lock before it checks whether profiling is on. Every worker queues for that lock on each surface height computation, whether anyone is profiling or not. It cost 0.5 to 0.7% of worker time.

## A quirk vanilla depends on

The most careful analysis went into the older noise that shapes the overworld's base terrain. It fills a whole grid of points in one batched pass and keeps a small cache from one point to the next. The overworld runs it in a compatibility mode that lets that cache carry over from one column of the grid into the next. When a new column starts at the same height as the last cached point, its first point reuses the previous column's values.

That leak is part of vanilla terrain. A faster version that computes each point on its own would build a slightly different world. Any rewrite has to walk the grid in the same order and carry the same stale values forward. We decided to build it only behind a check that compares its output with vanilla's.

## The list

By the end we had ten candidate changes, all designed to keep terrain identical. They included the shared surface height cache, working out each water grid point's random position once instead of for every block, and skipping structure pieces that cannot reach a block. On paper, seven smaller changes plus a faster single-value noise routine would cut 26 to 30% of the time spent on generation itself, leaving out the scheduling work, about 1.35 to 1.4 times the generation throughput. Computing noise several values at a time, together with a batched rewrite of the older noise, could push that to 1.5 or 1.6 times.

Every replaced function depends only on the seed, the position and a few fixed inputs, so a shadow mode can run vanilla's version on a sample of calls and log any difference. Matching bit for bit means doing the floating-point math in exactly vanilla's order.

## What shipped

None of it, so far. While we planned the noise work, a stack sampler on every worker during a 100-player join showed that four or five of the seven generation workers barely ran. That fits the 2.7 cores in our first profile. The pool woke a sleeping worker only after a large backlog had built up, and the scheduler rarely queued enough work to reach it.

That hunt has its own post, [Seven terrain workers, and most of them asleep](client-vs-server.md). In a 100-player A/B on a 0.11.14 development build, lowering the wake threshold to a small value cut the median view load from 132 s to 86 s. Endstone 0.11.14 sets it to zero, so any queued task wakes an idle worker (`cd97eea16`). Once four or more workers were busy, view load stayed at 83 to 86 s.

Inside the generator itself, Endstone 0.11.14 only raises the size limits on two caches the server used to wipe whenever they filled. In a later release benchmark against vanilla BDS 1.26.52, with 100 players on Windows, the median view load was 81 s on Endstone and 192 s on vanilla. Faster terrain math would mostly give the workers more idle time. The list stays on the shelf until generation becomes the limit again.

## What we learned

The 2.7 cores were in our very first measurement. We read them as the cost of terrain. They were really telling us how many workers were awake. A profile only sees the threads that run, and four or five idle workers left almost no trace in it.

We also never had a count of chunks generated per second, the number the whole list was meant to move. We want that number in hand before the list comes off the shelf.
