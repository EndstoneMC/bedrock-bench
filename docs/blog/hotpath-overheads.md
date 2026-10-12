---
title: "Every mob was asking about every player"
date: 2026-10-12
summary: "We went looking for per-player work inside the code every mob runs, and found a cost that our old mob-free benchmark had multiplied by zero."
tags: [performance, profiling, benchmarks, mobs, endstone-0.11.14]
---

Our earlier load tests ran on a flat world with no mobs. In early October we moved them to a freshly generated world, with every player spread far from the others so that no two share a chunk. Every player has its own patch of terrain to generate and tick. That is a hard case for the server, and much closer to a survival world than the flat world ever was. This post is the companion to [Walking the player list 2,500 times a tick](opt-player-scans.md), told from the mobs' side.

On that workload, after [our autosave fix](chunk-storage.md), the main server thread took about 48 ms per tick with 60 spread players. Nothing new stood out in the profile. Random ticks and redstone sat at the top, and below them was a long tail of small items worth a few percent each.

One of those small items bothered us. The routine that visits every online player, together with its bookkeeping, took 7.8% of the main thread. That is close to 4 ms per tick for a loop over 60 entries. Something had to be calling it very often.

A sampling profiler only sees the load you run it under. If that cost grew with the square of the player count, 4 ms at 60 players would become about 11 ms at 100 and about 24 ms at 150, half of the 50 ms a tick is allowed to take. We wanted to know how it grew before we trusted the small percentage.

## Starting from the mobs

We set the profiler aside and made a list of what runs for every entity on every tick: mob ticks, movement and pushing, despawn checks, and every AI goal deciding whether it wants to act. We followed each of those into the shared code it calls and asked one question of each piece. How much work does this do per player?

Several shared routines look like one cheap call where they are used. Each of them does work for every player in the world.

The clearest case was the query that finds entities in an area. It gathers the entities in the nearby chunks, then appends players by walking the full player list. Mob pushing uses it. So do collision checks, AI target searches, hoppers and minecarts. Each of those calls costs time in proportion to the number of players online, even when the nearest player is a thousand blocks away.

The player walk itself covers the whole level. A mob in the Nether steps through every player in the Overworld to find the ones in its own dimension. Each step does some thread-safe bookkeeping and a few lookups to confirm the entry is a live player. Dozens of places call this walk, and some of them run on behalf of mobs: despawn checks, and animals that follow a player holding their food. Pets that keep track of their owner do a similar scan of their own, because finding a player by id also checks the players one at a time.

Sending an entity update had the same shape. For every packet about an entity, the server asked each player whether that entity was relevant to them, with several lookups per player.

None of this is a bug. With a handful of players it costs almost nothing. It only gets expensive when you multiply entities by players.

## Zero times anything

This part stung. On our old flat world, every term that scales with mobs times players was zero. Mob pushing and AI goals had nothing to do, and entity updates only covered the players themselves. The parts that pair players with players still ran there, such as the check each player makes once a tick for anything it is touching. The mob-driven part of the cost was invisible.

The generated world has mobs, so those terms are real. In our runs the number of mobs did not grow much with the player count, and the mob-driven calls barely moved between 60 and 100 players. On a server where mobs grow with players, their total grows with the square of the player count too.

To count the walks we priced a single visit from one caller whose call count we knew, then turned every other caller's time into a count. The [player-scan post](opt-player-scans.md) shows how. At 100 players the server did the equivalent of about 2,500 full walks of the list per tick, and the loop that had cost close to 4 ms at 60 players now cost 10.3 ms. That is close to the 11 ms we had projected.

The extra walks came from players and what sits near them. Catch-up passes over player input walked the list more often, and so did the touch check, updates about the players themselves, and spawners and minecarts near each player. Part of that came from the low tick rate. At about 10 TPS, half of all player input lands in catch-up passes, so input-driven work looks bigger per tick than it is.

## What we changed

The player list changes when someone joins or leaves. Endstone now keeps a snapshot of it, rebuilt when it changes, with each entry already resolved to its player. Each walk checks that the player is still in the game and in the right dimension, and any walk the snapshot cannot serve safely goes to vanilla's own code. For entity updates we read the entity's id once and look it up in each player's set of nearby entities. Both changes affect timing only. Every caller sees the same players it saw before.

We tried the idea first as a prototype on BDS 1.26.51 with 100 spread players. It went a little further than what we shipped. That build already had our autosave fix and a terrain blending prototype loaded, and it ran at 12.5 TPS. The player-scan changes took it to 13.4 TPS and cut main thread time from 80.2 to 74.6 ms per tick, a saving of 5.6 ms.

To check the shipped change, we built a version of Endstone that runs vanilla's code next to ours on every call. It found no differences in tens of millions of walks. The [player-scan post](opt-player-scans.md) has the details of both runs.

The change shipped in Endstone 0.11.14 as commit fa7b3d4eb. On our Windows benchmark with 100 spread players, vanilla BDS 1.26.52 runs at 9.4 TPS with 105 ms of main thread per tick. Endstone 0.11.14 on the same BDS build holds 20 TPS at 43 ms, with this change and the rest of the release.

## What we do now

Next to every profile, we now audit the per-entity tick work for hidden per-player costs. Every benchmark we run now uses a generated world with spread players and the mobs that come with it.
