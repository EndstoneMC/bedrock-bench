---
title: "22 ms to find 0.73 ms of work: warming the cache for random ticks"
date: 2026-10-12
summary: "Random ticks were the largest single cost on our 100-player server, most of it spent waiting on memory, so Endstone 0.11.14 starts those memory reads early and leaves the random-tick loop as it was."
tags: [performance, random-ticks, cpu-cache, profiling, endstone-0.11.14]
---

Random ticks are how a Minecraft world changes on its own. Every tick, the server picks a few random blocks in each chunk near a player and gives each one a chance to act. Grass spreads, crops grow, leaves decay and ice melts. Most picks land on stone or air, and nothing happens.

We expected random ticks to be cheap. Once we had fixed autosave, they turned out to be the most expensive thing our 100-player server did.

## Where the time went

Our benchmark spreads 100 players across a fresh world at simulation distance 4. Each player keeps 57 chunks ticking, so the server random-ticks about 5,700 chunks every tick. At the default random tick speed on ordinary terrain, that comes to about 25 picks per chunk, or roughly 140,000 block picks per tick.

At first [the autosave picker](chunk-storage.md) hid all of this. At 60 players it was eating close to half the main thread. Once we fixed that, the chunk tick, where random ticks live, rose to the top of every steady 100-player profile at between a quarter and just over a third of the tick.

One of those profiles gave us the numbers in the title. The whole chunk tick took 32.5 ms per tick. About 22 ms of that was random ticking. The blocks that actually did something accounted for 0.73 ms. Everything else went into picking blocks and finding out they had nothing to do.

## A loop that waits

The profile did not help much at first. The compiler had folded the random-tick loop into the larger chunk tick, so our sampling profiler showed it as one block with no detail inside.

We split it ourselves. Each sample records where the processor was in the code, so we sorted the samples by which part of the chunk tick they landed in: weather, the search for the highest section that holds blocks, the sampling loop, and each step inside the loop. One line became about 30.

The new rows pointed at memory. The check that asks whether a block reacts to random ticks runs more than once per pick. The first call cost about five times as much as each later one. The later calls showed what the check really costs, and the difference was the first call waiting on a cache miss for the block's data.

To find the block at a random position, the server follows a short chain of dependent memory reads, from the chunk down through its section and block storage to the block itself. Each read needs the result of the one before. For a position nobody has touched recently, each read goes all the way to main memory. By the time a chunk comes round again, its data has long left the cache. At about 140 ns per pick, the loop spent most of its time waiting.

A modern processor can usually hide some of that wait by starting the next iteration's reads early. Here it could not. The random number generator that chooses each position wrote its seed in a way that forces the processor to finish every outstanding memory read first. So each pick's cache misses queued up behind the previous pick's.

That changed how we estimated fixes. The cost of a pick was roughly the number of dependent misses times the memory latency. Trimming instructions would barely move it. The misses had to overlap. Our first step was a small patch that removed the wait on the seed write.

## The plan we did not build

Next we designed a replacement for the random-tick loop. It would skip sections where no block can random tick, such as solid stone or open water, and overlap the reads that remained.

Working through it taught us two things. Whether a block random ticks is not obvious from what it is. Lava random ticks and water does not. Redstone ore only ticks while it is lit. Any skip test would have to ask the game's own code about every block.

Skipping was also weaker than it looked. To know that a section cannot tick, you have to read the list of blocks it contains. That read costs about as many cache misses as the two or three picks it would save. Most of the gain had to come from overlapping the misses that remained.

There was also upkeep to consider. Replacing code inside a compiled function would need re-checking on every BDS build. We looked for something smaller.

## Reading ahead

What shipped is a single hook. Just before the random-tick loop, the chunk tick runs mob spawning. We hook the chunk tick and run a warm-up right after spawning finishes. The warm-up copies the random seed, runs the same position math the loop is about to run, and asks the processor to start loading the memory for those picks all at once, one link of the chain at a time. It handles up to 256 picks per chunk, which covers every pick at the default random tick speed.

Then the original loop runs exactly as it always did. The data it needs is already on its way into the cache.

The warm-up only reads memory. It never advances the game's seed and never changes which blocks tick or in what order. A wrong prediction would cost speed and nothing else.

The Windows and Linux builds gave us one surprise. They compute the block position from the random number differently, and for about half of all seed values they pick different blocks. A prediction copied from one platform would have warmed the wrong memory on the other, so ours follows each platform's own math.

## What we measured

We did not give the warm-up its own A/B run, so we have no clean before and after for it alone. Our estimate from the profile was 8 to 12 ms per tick at 100 players, because about 17 of the 22 ms were misses waiting in line behind each other.

The seed patch went in first (`036a4adc8`) and the warm-up followed (`6e7417c00`). We expected the warm-up to cover most of what the patch saved, so we dropped the patch before the release (`f1f0b65f1`). A later 100-player run without it stayed within run-to-run noise of the build that had both, about 15 ms per second, or under 1 ms per tick. That run also carried four network changes, so it is not a clean test of the patch alone. Still, we saw no measurable change on the main thread.

The warm-up is one of about ten performance changes in Endstone 0.11.14. Together those changes take the main thread from about 105 ms per tick on vanilla BDS to 43 ms on Windows and 46.5 ms on Linux, and steady TPS from about 9.4 to 20.

The lesson we keep from this one is about estimating. Count the dependent cache misses before guessing what a fix will save. When the first of several identical calls costs five times the rest, it is paying for the miss.
