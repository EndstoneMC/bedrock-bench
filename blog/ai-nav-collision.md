---
title: "Who is the nearest player?"
date: 2026-10-12
summary: "We went looking for the cost of mob pathfinding and collision and ended up at one question that 22 places in the server keep asking: where is the nearest player?"
tags: [performance, mobs, pathfinding, profiling, endstone-0.11.14]
---

When a Minecraft server slows down, mobs get blamed first. Pathfinding and collision have a bad reputation on every edition of the game, so that is where we started looking when we set out to make Endstone handle more players.

Our test load is harsh on purpose. One hundred players join a fresh world at the same moment, each on its own patch of land far from everyone else, with view distance 10 and simulation distance 4. Every player keeps the chunks around them simulated, so mobs spawn and get ticked around every one of them. Vanilla BDS 1.26.52 manages 9.4 TPS on that load. Its main thread spends about 105 ms on each tick, and a tick only has 50.

So we followed mobs through a tick.

## What a mob does in one tick

A mob's tick starts with its goals, which choose what the mob wants to do next. The rest of the tick moves it there without letting it walk through walls or other mobs.

The pathfinder has a hard cap on how many spots it will try. A mob that cannot reach its target gives up after a fixed amount of effort, so one stuck zombie cannot eat a tick.

Collision had a gate too, but only for part of the work. The expensive check for bumping into solid entities such as boats and shulkers only runs for mobs that a cheaper pass has already flagged as having one nearby. Pushing has no such gate. Every mob looks for neighbours to push against on every tick, so even a cow alone in a field pays for that query.

Goals ran more often than we expected. The code that picks a mob's next goal checks every idle goal on every tick. Most idle goals have no throttle. A few have cooldowns, and the goals that scan for a target stagger their scans across mobs.

There were small things. The behaviour that makes a mob flee from another kind of mob runs every tick, and it fetches and sorts the same list of neighbours once for every kind of mob it avoids, two or three times over for rabbits and foxes. The pathfinder allocates memory for each spot it considers. We wrote both down.

Later we profiled 60 spread players with [our autosave fix](chunk-storage.md) loaded, at about 48 ms per tick, and mob goal selection came to 5.9 percent of the main thread. That is real work, and it grows with the number of mobs. In our runs the mob count barely grew with the player count, so goals could not explain why going from 60 to 100 players roughly doubled the time per tick.

## The thread we kept pulling

Another lead was a question that kept coming up in unrelated places.

A cow following someone holding wheat asks where the nearest player is. So does a wolf begging for a bone. An experience orb asks so it can drift toward whoever stands closest. Spawners and beacons ask too, and a zombie deciding whether to despawn uses the same code to check whether any player is near. When we finished counting, 22 places in the server were asking that same question, several of them in code that runs every tick for whole groups of mobs.

The nearest-player question walks the server's player list. A second, busier route ends at the same walk. When a mob looks for what is near it, to push against it or to pick a target, the server gathers the entities in the nearby chunks and then walks the player list to add any players in range. That is the ungated push query every mob runs each tick. Collision checks, hoppers and minecarts use the same query. Sending a packet about a mob whose data changed walks the list once more, to decide which players should receive it. The 22 nearest-player questions are one group among the dozens of places that walk the player list.

That list holds every player on the server, in every dimension. A pig in the Overworld still steps through all 100 players, including any in the Nether. Each step also does a little bookkeeping to confirm the entry is still a live player.

In our 60-player profile, the walk and its bookkeeping took 7.8 percent of the main thread across every caller, not far above goal selection. Its cost grew differently. Every walk visits every player, and mobs and players both start walks, so the walk grows with callers times players. From 60 to 100 players, visits rose from about 94,000 to about 249,000 per tick, worked out from the time each caller spent, and the walk's cost went from 3.85 ms to 10.28 ms.

The mob-driven calls barely changed between the two runs. The extra walks came from callers driven by players, such as their input and the spawners near them. The walk accounted for about 6.4 ms of the extra time per tick, one cause among several.

## One fix for every caller

We left every mob behaviour exactly as it was and replaced the walk underneath them. Endstone keeps a ready copy of the player list, rebuilt only when the list changes, and each walk still checks that a player is alive and in the right dimension before handing it over. The same commit gave packets about an entity a cheaper check for which players should receive them.

It is a timing-only change. Every caller sees the same players in the same order. How we counted the calls, and how we checked every answer against vanilla's, is in [Walking the player list 2,500 times a tick](opt-player-scans.md).

The change landed on a Saturday night as commit fa7b3d4eb.

## What it bought

We tried the idea first as a prototype on vanilla BDS 1.26.51 with 100 spread players, with our autosave fix and a terrain blending prototype already loaded. That server ran at 12.5 TPS with 80.2 ms of main thread per tick. The prototype took it to 13.4 TPS and 74.6 ms, 5.6 ms off every tick and more than a tenth of the 50 ms budget. Besides the cached list and the cheaper recipient check, it also sped up two smaller lookups that the release left out.

Together with the other fixes in the release, Endstone 0.11.14 runs the full 100-player test on BDS 1.26.52 at 20.0 TPS. The main thread spends 43 ms per tick on Windows, against 105 ms for vanilla, and 46.5 ms on Linux, against 105.7 ms.

## What we learned

We now rank a hot spot by how its cost grows as well as by its share of one profile. At 60 players the player walk sat close to goal selection. Going to 100 players, its cost grew 2.7 times while the player count grew 1.67 times.

We also stopped guessing how often things run. When we cannot count calls directly, we work them out from timing and say so.

Our benchmark still has limits. It covers natural mob spawns around 100 spread-out players. Large mob farms are not part of it yet, and an entity-dense workload is on the list for the benchmark suite.
