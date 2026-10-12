---
title: "Walking the player list 2,500 times a tick"
date: 2026-10-12
summary: "Between 60 and 100 spread players, one small loop over the player list grew 2.7 times in cost, and Endstone 0.11.14 now walks a cached copy that gives the same answers."
tags: [performance, profiling, players, endstone]
---

In [an earlier post](chunk-storage.md) we fixed the autosave picker, which had been taking 45% of the main thread. With that fix loaded into vanilla BDS, 60 players spread across a fresh world ran at 20 TPS. At 100 players the same server fell to 10.4 TPS, and the main thread spent about 96 ms on every tick. Some growth was expected. We wanted to know what had grown faster than the player count.

## Asking everyone, every time

The sampling profiler kept landing on one small piece of code. It answers a simple question: which players are in this dimension? It walks the whole player list and hands each matching player to whoever asked.

A lot of code asks. Mobs ask when they push into each other, when they decide whether to despawn, and when they look for a player holding food. Spawner blocks ask. Minecarts ask. Every packet about an entity whose data changed asks, players included, so the server can decide who should receive it. Each player checks once a tick for anything it is touching, and that check walks the list too. We counted about 20 distinct callers in the profile.

Each visit does a few bookkeeping checks to confirm the player is still in the game and in this dimension before the caller's own code runs.

At 60 players the loop cost 3.85 ms per tick. At 100 players it cost 10.28 ms. The player count had grown 1.67 times and the loop had grown 2.7 times.

## A ruler for the loop

Time per tick did not tell us how often the loop ran. For that we needed one caller whose call count we already knew. The touch check runs once per player per tick and walks the full list each time, so at 100 players it makes exactly 10,000 visits a tick. Its time divided by that count gave about 21 nanoseconds per visit, and with that figure we turned every other caller's time into a call count.

The answer was about 2,490 walks of the list per tick at 100 players, up from about 1,570 at 60. Visits rose from 94,000 to 249,000 per tick, a factor of 2.65, close to the 2.78 that pure quadratic growth would give. The visits alone cost 5.35 ms per tick before any caller did real work.

Most of the growth came from player input, packets about the players' own updates, spawners near each player and chunk requests from clients. Calls driven by mobs barely moved, because the number of mobs did not grow with the number of players in our runs.

## The table that lied

Partway through, a table comparing three profiles showed some odd numbers. The bug was in our own tooling. The script that turns raw samples into call stacks cached them by the identity of the loaded profile, and a newly loaded profile could inherit the identity of the one Python had just freed. Every column after the first showed the first profile's stacks, so none of our 60-to-100 comparisons could be trusted until we fixed the cache.

## A ninefold rise that was not real

The corrected tables held one more surprise. The time the server spent catching up on player movement went from 0.66 ms per tick at 60 players to 5.69 ms at 100, almost nine times as much.

Clients send input 20 times a second whatever the server's tick rate. The server gives each player 20 input slots per second of real time, handles one input per player during each tick, and works through the rest in catch-up passes afterwards. At 20 TPS almost nothing is left over. At 10.4 TPS about half of all input lands in catch-up.

Measured per second instead, the movement work that only involves players grew 1.4 to 1.7 times for 1.67 times the players. That is linear growth. Any work driven by client input looks worse per tick as TPS falls, even when its real cost stays the same.

The part of catch-up that did grow faster, about 1.9 ms per tick, was the same player-list walk again.

## The fix

The player list changes when someone joins or leaves. Between those changes it is read thousands of times. We now keep a snapshot of the list with each player already looked up. Every call first compares the snapshot with the live list and rebuilds it only when they differ. We estimate that comparison costs about 0.1 ms per tick across all calls.

Callers get players in the same order as before and can still stop early. We still confirm each player at call time, because someone can stay on the list for a moment after leaving, and any walk the snapshot cannot serve safely goes to vanilla's own code.

Packets about an entity got a second shortcut. For each of those packets the server asked every player whether that entity was relevant to them, and worked out the entity's id again for each player. We work out the id once and look it up in the set of entities each player is already tracking. In our test about 99 of every 100 answers were no. Our lookup matched the server's own hashing on all 31,017 test ids, on both Windows and Linux.

We also count active players from the same snapshot. The server ran that count once per player per tick, which made it quadratic too.

## What it bought

We tried the idea first as a small test build loaded into an unmodified vanilla server, with the same seed and the same 100 bots. That build also sped up two smaller things we left out of the release: the check for which players should get an entity's movement updates, and looking up a player by id. It saved about 5.6 ms per tick against our estimate of 5.5. This A/B run had its own vanilla baseline of 10.75 TPS, and together with three other fixes built the same way, the prototypes raised it to between 19.0 and 19.7 TPS.

None of this should change what happens in the game. To check, we built a version of Endstone that runs vanilla's code next to ours on every call and compares the results. Over a whole Linux run with 100 bots, including 15 minutes of chaos, it found 0 differences across 47.8 million walks of the player list, 3.6 million entity packets and 1.7 million player counts. The Windows run compared 25.1 million walks, 2.19 million packets and 0.95 million counts, also with 0 differences.

The change shipped in Endstone 0.11.14 as `fa7b3d4eb`. With the rest of that release, our benchmark on Windows holds 20 TPS with 100 spread players and spends 43 ms of main-thread time per tick. Vanilla 1.26.52 manages 9.4 TPS at 105 ms.

## What we learned

The step that unlocked this was the touch check. One caller with a known call count let us price every other caller, and the call counts showed us which ones grew with players. And when TPS is below 20, we now compare input-driven work per second, since per tick it looks worse than it is.
