---
title: "We thought the mobs would break it"
date: 2026-10-12
summary: "Our paper model put mob despawn checks at up to 15 ms per tick on a 100-player server, and the profiler measured about 0.7 ms."
tags: [performance, lighting, mob-spawning, profiling, bds]
---

Our earlier load tests ran on worlds with mob spawning, the daylight cycle and lighting updates switched off. The systems a survival server leans on hardest had never shown up in our profiles.

In October we moved to the benchmark we use today: 100 players placed far enough apart on a fresh survival world that no two share a chunk, with default game rules. Once we had dealt with the bottlenecks we already knew about, would lighting and mobs be the next wall?

We started on paper with lighting, natural spawning and despawning. For each piece we noted whether it runs per block change, per chunk, per mob or per player, and then put a rough cost on it.

## Lighting was already in good shape

Lighting was the first surprise, and a pleasant one. Bedrock clients compute light themselves, so the server never sends it. The server still needs it for gameplay: spawn rules, crop growth, ice and snow, daylight sensors.

BDS handles it well. A block change only queues lighting work when it changes how much light the block gives off or lets through, so most edits cost almost nothing. Queued changes are batched per 16x16x16 chunk section. The relighting that runs on the main thread works to a small time budget on each pass and leaves the rest for later ticks. New terrain gets its first lighting on the chunk generation workers, away from the tick. Light spreads in order of brightness, which is the core idea behind Starlight, the well-known lighting rewrite for Java Edition.

We did find room to improve. Each relight does a lot of fixed setup, and its inner loop looks up neighbouring blocks through a general-purpose path. A tighter loop with identical results is possible. Merging neighbouring sections into one bigger pass, as Starlight does, could change light levels where the result depends on the pass boundary. We count that as a gameplay change, so it stayed off the list.

The profiler settled the question anyway. In steady state, lighting came to between 0.0 and 0.2 percent of the main thread. We left it alone.

## Spawning was rare and thorough

The natural spawner is cheap per chunk. Each chunk inside a player's simulation distance gets a small random chance each tick, well under one percent, to try a spawn.

When an attempt does happen, it does a lot of work. It recounts every mob in the surrounding area from scratch, then walks down the column looking for a place to spawn. Every cave it passes under the surface triggers another full recount, even when nothing spawned in between.

One detail mattered for any future fix. Every ticked chunk takes one number from the world's shared random sequence, whether it spawns anything or not. Skip that draw and every random event after it shifts. A faster spawner would have to keep it.

We estimated the spawner at about 1 ms per tick at 100 players. The 60-player profile already showed about 2.1 percent of the main thread, roughly 1 ms per tick, so that estimate was low. It was still a small slice of the tick, so we moved on to the part of the model that worried us.

## The prediction that missed

Despawning looked like the big one. Every mob that can despawn checks, every tick, whether a player is close enough to keep it alive. It asks up to three questions, and each one goes through the list of online players: is there any player at all, is one within 32 blocks, and is one within 128. That is mobs times players work, and on a spread server we expected both numbers to grow together.

Our first worry was a despawn filter that measures distance to the nearest player. It scans every player with no early exit. Before ranking it, we counted in the vanilla behaviour packs that ship with every server. The files are not strict JSON, so we scanned them with a small script. 73 of the 76 despawn definitions use the built-in distance rule. Only 3 use filters. The built-in rule was the one worth measuring.

With 1,000 to 3,000 mobs on a 100-player server, each walking the player list a little more than once per tick, our model put despawn at 5 to 15 ms per tick. It was the largest number in the whole analysis, and we were ready to write the fix.

The profiler disagreed. At 100 players, despawn's player lookups cost about 0.7 ms per tick. Our estimate was between seven and twenty times too high.

The miss came almost entirely from the number of checks. Our model meant about 1,250 to 3,750 full walks of the player list per tick at 100 players. A lookup stops at the first player close enough, so we read the profile as full-walk equivalents: about 250 per tick. That is 5 to 15 times fewer than we had planned for. It also stayed flat at about 250 from 60 to 100 players.

The other guess held up. With the check itself included, each player visit cost about 29 ns, at the low end of the 30 to 50 ns in our model.

## The same question, all over the server

The despawn check is one of 22 behaviours that ask which player is nearby. Cows and sheep ask it to decide whether to follow a player holding wheat. Spawner blocks, beacons and enchanting tables ask it as they tick. XP orbs ask it to pick a target.

Those 22 sit on top of a single walk over the player list that dozens of features share. At 100 players the nearby-player checks added up to about 2 ms of that walk's cost. Most of the rest came from mob pushing, entity queries, choosing which players receive each mob's packets, and motion sync.

We wrote up that walk and the fix in [Walking the player list 2,500 times a tick](opt-player-scans.md). Endstone 0.11.14 answers it from a cached copy of the player list, and every caller sees the same players in the same order as in vanilla.

## What we took away

We still do the paper analysis first, because it tells us what each cost grows with. We no longer rank fixes by it until a profile agrees. The despawn estimate rested on two guesses, how many checks run per tick and what each player visit costs. The cost guess was close. The count guess was 5 to 15 times too high.

Counting the despawn definitions in the shipped behaviour packs told us which rule to measure before we wrote any code. We now read the shipped data before we call a code path hot.

On our 60-player profile, lighting and the natural spawner together came to about 1 ms per tick. The next wall was random block ticks, which we wrote up in [22 ms to find 0.73 ms of work](opt-random-ticks.md).
