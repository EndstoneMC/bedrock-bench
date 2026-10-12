---
title: "What decides whether a cow ticks"
date: 2026-10-12
summary: "We mapped how the Bedrock server decides which mobs tick and what an idle mob does every tick, then chose which levers Endstone could pull without changing gameplay."
tags: [performance, entities, mobs, bds, endstone-0.11.14]
---

With the [autosave picker that slowed down with every chunk](chunk-storage.md) fixed, our 60-player test server held 20 TPS with the main thread at about 48 ms per tick. The budget at 20 TPS is 50 ms, so there was almost no headroom left.

Random ticks and redstone still led the profile. Below them sat a long tail of entity work. Mob goal selection took 5.9% of the main thread. The loop that visits every online player, together with its bookkeeping, took 7.8%, and mobs call it constantly.

There was also a question we could not answer. Does a mob far from every player cost less than one standing next to a player? On Java Edition, Spigot added an activation range that ticks far-away mobs less often, and Paper inherited it. We did not know whether Bedrock had anything like it. Before touching any entity code we wanted a map of which mobs tick and what an idle one does each tick.

## Several hundred small jobs

Bedrock runs most of its entity logic as an entity component system. By our count the server has several hundred small systems, each with one job. One applies gravity. Another lets each mob pick its next goal, such as wandering off or following a player who holds wheat. Each system runs once per tick over every entity it applies to.

They run on the main server thread, one after another, in a fixed order. We started by writing that order down, so that any line in a profile could be placed in it.

Mob behaviour no longer lives inside each mob. Goal selection and movement are separate passes over every mob that needs them. A mob's tick is spread across many systems, so no single line in a profile holds it.

Writing the order down led to our first wrong turn. One of our analysis tools quietly cut long output short, and the main tick is long enough to hit that limit. We only ever saw its first half. The entity systems live in the second half.

For a while we believed the main tick never ran the entity systems, and that mobs had to be updated from somewhere else. Had we kept going, our map would have started with no mobs in the main tick at all.

Once we raised the limit, the entity systems were right there in the half we had missed. We now check every tool's output for a silent cut-off before we trust it.

## One flag per tick

The answer to "which mobs tick?" came quickly once we changed the question. Reading the hundreds of systems that act on a ticking mob would have been slow. We asked what makes a mob ticking in the first place.

Each tick, the server flags the entities it will simulate. A large share of the entity systems check for that flag, or for a related one, and skip anything without it. Only a few places set the flag. Chunk ticking flags the entities in every chunk it ticks. The code that handles riders and their vehicles sets it too. A short list of special entities is flagged wherever they are.

That answered our question. Bedrock has no activation range. Every mob in a ticking chunk runs at full rate, whether the nearest player is two blocks away or sixty. Chunks tick inside each player's simulation distance and inside ticking areas. Apart from that short list of special entities, mobs anywhere else are frozen. For a server admin, simulation distance and ticking areas are the only settings that change which mobs tick.

## What an idle cow does

With the map in hand we followed a single cow standing in a pen. It does nothing interesting, and goal selection still runs for it on every tick. On Java Edition, running goals update every tick, and full goal selection runs every other tick.

Tempt, the goal that makes a cow follow wheat, asks every player whether they are holding some. The despawn check asks about players too.

That player question turned out to be everywhere. We found 22 per-tick behaviours that walk the player list, including despawn checks, tempt, wolves begging for food and experience orbs.

The player list became its own investigation, which we wrote up in [Walking the player list 2,500 times a tick](opt-player-scans.md). Endstone 0.11.14 keeps a cached copy of the list, and every caller still sees the same players in the same order.

## The levers we left alone

The map also showed two bigger levers. We chose not to pull either.

The tick flag looks like the natural place for an activation range. Leave it off far-away mobs on most ticks and they would skip most of their update. We never built it. Paper does something similar, where far mobs get a minimal tick and wake up now and then. On Bedrock it would change the game. Far mobs would move and breed more slowly, and the farms built around them would slow down.

Spreading goal selection over two ticks, the way Java does, would save less. A mob could start a goal up to one tick later than it does today. That is still a gameplay change.

Endstone keeps vanilla gameplay, so neither went into 0.11.14. The player-list cache did, because every caller gets the same answers as before.
