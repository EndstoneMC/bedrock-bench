---
title: "What does one more player cost?"
date: 2026-10-12
summary: "We counted how most of the per-player loops in BDS grow, priced their steps with small replica programs, checked the results against a profiler, and found a walk over every player hiding inside the work each mob does."
tags: [performance, scaling, benchmarks, bds, endstone-0.11.14]
---

In early October we put 60 players on vanilla BDS 1.26.51, spread across a fresh world at default settings, including a view distance of 32. The server held about 11 TPS. The main server thread spent around 85 ms on each tick, against a budget of 50. We wanted 100 players at 20 TPS, so we needed to know what each extra player adds.

The locator bar showed why a single profile could not tell us that. It cost 3.2 ms per tick at 60 players and 19 ms at 120, measured while the second 60 were still joining. Twice the players gave roughly five to six times the cost, and the join spikes inflate the 120-player figure.

Bigger crowds are also slow to profile. We estimated that 120 spread players would need more than half an hour just to load every view. While the profiles ran, we started counting.

## Putting a price on a loop

For each loop whose length depends on the player count, we asked how many items it visits and what one visit costs. Our unit was the lookup. Each time the server goes from an entity to one of its properties, it pays for a small search. A loop step became a number: three lookups here, five there.

Containers had prices too. The locator bar keeps a table of player pairs sorted by a hash, and it recomputes that hash at every comparison. A search costs about 2 log2 n hash computations. With 100 players there are 9,900 pairs in the table, so each search costs about 27 of them. That is how a log creeps into a cost that otherwise grows with the square of the player count.

Player movement, the largest cost that grows in a straight line with players, was too tangled to size by hand, so we left it to the profiler.

## Replicas

To turn lookups into nanoseconds we wrote replica benchmarks: small standalone C++ programs that rebuild a loop's skeleton and time it. A replica keeps the loop's data structures and allocations and leaves the gameplay out, which barely changes how the cost grows.

The locator bar's pair check came out at 250 to 350 ns per pair. For that one part, that is 2.4 to 2.8 ms per tick at 100 players.

Each replica ran twice at high priority, pinned to a different performance core each time, and we quoted the range. Two runs of one loop once differed by a factor of two, 2.2 ns against 4.7 ns per step.

We treat every replica as a floor. It skips the entity lookups and runs with its data warm in cache, so a real server pays more per step. Added together, our replicas put the locator bar at 5 to 8 ms per tick at 100 players. The profiler measured about 11.

## Three wrong numbers we almost wrote down

The first was a loop that looked like core player logic. We priced it before reading its first branch, which on a dedicated server sends the code elsewhere. The path we were costing runs only in the game client.

The second went the other way. The locator bar draws each dot through a routine with one version per kind of object. The player-dot version looked lean, so we first priced it with no heap copies. At its end it hands off to the general version, which copies the dot's texture path twice on the heap for every waypoint on every tick. Removing those copies became part of the locator bar fix.

The third was size. One of the largest routines a player runs each tick looked like a hot spot. Most of it handles respawning and only runs when a flag is set. The part that runs every tick is a short loop and one heap copy.

## Checking the shape

The counting gave the locator bar a shape: one part grows as N² log N, one as N³ and one as N². We sized each term from the 100-player profile and asked the model about 60 players. It predicted 3.25 ms per tick. The profiler had measured 3.18 ms.

The shape held at a point it had not been fitted to. At 200 players the vanilla locator bar alone would take about 60 ms per tick, more than the whole tick budget.

## The walk inside every mob

One routine kept turning up in separate investigations: the walk over every online player. One visit costs about 21 ns. With a helper that resolves each entity it finds, the walk took 3.85 ms per tick in a 60-player run at 20 TPS. At 100 players and 10.4 TPS it took 10.28 ms, about 1.66 ms of that in the helper.

The surprise was that most calls did not start in player code. When a mob checks whether it can despawn or pushes against its neighbours, it asks the world about nearby entities, and each of those checks walks the list of every player on the server. Spawners and spawn attempts around each player do the same. At 100 players this added up to about 2,490 walks and 249,000 player visits per tick, up from 94,000 at 60.

The length of each walk grows with players, and the number of walks grows with mobs and with the area around each player. The visits rose 2.65 times from 60 to 100 players, close to the 2.78 a pure square would give. The mob-driven calls barely changed between those runs. Where the mob count rises with the player count, those become square terms too.

Reading the code that sends chunks to players turned up a quieter cost. While a player has chunks waiting, their queue is re-sorted in full on every tick. At 100 players that took 0.19 ms per tick in steady state and 2.0 to 2.5 ms in the heaviest phases of the run.

## Prototypes and the release

We wrote four prototype patches and tested them on BDS 1.26.51 with 100 spread players at view distance 10 and simulation distance 4, on top of our earlier autosave fix. The baseline was 10.75 TPS at 93 ms per tick.

A cached player list, plus a faster check for which players need each mob update, saved 5.6 ms per tick ([Walking the player list 2,500 times a tick](opt-player-scans.md)). The locator bar patch saved 7 ms ([9,900 pairs a tick](opt-locator.md)). The chunk-sending patch gained mostly by setting aside chunks that cannot be sent yet and by not repeating a per-player update ([Parking the chunks that are not ready](opt-publisher.md)). The fourth remembers whether a chunk needs terrain blending, the smoothing of new terrain into older chunks, instead of working it out each time. With all four, the server reached 19.0 to 19.7 TPS at about 50 ms per tick. Random block ticks were then the largest cost left, at 22 ms.

The finished patches shipped in Endstone 0.11.14, along with the autosave fix and work on random ticks, packet handling, compression and terrain generation. On our 100-player Windows benchmark, vanilla BDS 1.26.52 runs at 9.40 TPS with 105 ms of main thread per tick, and Endstone 0.11.14 on the same build runs at 20.0 TPS with 43 ms. Those figures cover the whole release.

## What we took away

Counting predicted the shape of each cost before any profile was big enough to show it. The replicas came in low, so we treat them as floors and let the profiler set the price. Every number we nearly got wrong came from not tracing the path a dedicated server takes, and we now trace it before counting anything.
