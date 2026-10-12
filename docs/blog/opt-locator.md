---
title: "9,900 pairs a tick: cutting the cost of the locator bar"
date: 2026-10-12
summary: "The locator bar's cost grew faster than the square of the player count, and checking our fix against vanilla turned up a bug in vanilla's Linux build."
tags: [performance, locator-bar, equivalence, linux, endstone-0.11.14]
---

On the locator bar, each player sees a dot for every other player, placed along the top of the screen in that player's direction. On a small server it costs almost nothing. We wanted to know what it costs at 100 players.

To keep those dots right, the server considers every ordered pair of players, one viewer and one observed. With 100 players online that is 9,900 pairs, checked every tick. Nearly all of those checks end with nothing to send.

## The symptom

We first noticed the locator bar while profiling vanilla BDS with bots spread out. At 60 players it cost 3.2 ms per tick on the main server thread. That was small next to the [autosave problem](chunk-storage.md) we were chasing then. A steady capture at 100 players put it at 10.9 ms per tick.

Two thirds more players made it 3.4 times as expensive. Plain pairwise work would have made it 2.8 times. Something in there grew faster than the square of the player count.

## Splitting the profile

We took the sampling profile apart frame by frame. The locator bar time fell into separate piles: memory allocation, memory copying and comparing, looking up the entity behind each player, and the position math itself. The math was the floor. Everything else could go.

Then we worked out how each part grows with the player count. Looking up each pair's record grew with N² log N. Updating every dot on every player's bar grew with N². A third part, which decides whether anyone needs a new dot, grew with N³. We fit the model at 100 players, and it predicted the 60-player cost within a few percent. At 200 players it put the locator bar at about 60 ms per tick, more than a tick's whole 50 ms budget, with about 27 ms of that in the N³ part.

## Two surprises

The first surprise was a table. We had assumed it held records only for pairs of players far enough apart to need special handling. But when the server looks up a pair that is not there yet, it adds an empty record. So every pair ended up in the table regardless of distance. At 100 players that is 9,900 entries, searched again and again each tick.

The second was a short piece of text that the server copied for every pair on every tick, whether or not it had changed. It was too long to fit in the string's small built-in buffer, so each copy went to the heap. Comparing in place and copying only on change removed those allocations with no change in behaviour.

## Deciding what identical means

Endstone's performance work must not change gameplay, so we had to define identical. Vanilla gives each dot a random identifier, so the order of its packets already differs between two vanilla runs. We settled on identical up to that randomness, and we still let vanilla draw those identifiers in its own order.

## The fix

The fix keeps vanilla's decision code. In front of it sits a cheap check that proves, for a given pair, that this tick's work cannot change any packet, so we can skip it. Any other pair goes through vanilla's decision code, run against Endstone's own record for that pair, so the result is exact by construction.

The N³ part got the same treatment. On every player's update, the server walked the whole player list and, for each entry, scanned all of that player's existing dots to see whether one was already there. At 100 players that is about a million steps per tick. Our check collects a player's dots once and compares them against the list, about 200 steps per player. Only when a dot may need adding does vanilla's code run, in full.

Every lookup vanilla made along the way was read-only, so repeating them changes nothing. Floating point results depend on the order of operations, so our check calls vanilla's own distance test instead of a copy of it.

We left out one tempting idea, sharing one player's data across all of their viewers within a tick. A player can change between two viewers' updates, and the later viewer would then see it up to a tick late, so it would not be exact.

## Checking it against vanilla

We first built the fix as a small patch loaded into stock vanilla BDS, so the A/B compared one server binary with and without it. Then we ported it into Endstone with an instrumented build that runs vanilla's code next to ours for every pair and compares the packets byte for byte.

We put that build through 15 minutes of chaos: joins, kicks, teleports, deaths, dimension changes, game mode switches, and the locator bar game rule flipping on and off. On Windows the comparison covered 78 million pair checks and found zero differences.

On Linux, 98.5% of 101 million pair checks were proven to have nothing to send. About a quarter of the checks still disagreed with vanilla. In the cases we logged in detail, most were vanilla sending a location update where ours sent nothing.

We assumed the bug was ours until we compared table sizes. With 23 players online there are 506 ordered pairs. Our table had 506 entries. Vanilla's had 504.

## A bug in vanilla's Linux build

The Linux build of BDS files its per-pair records under a weak hash of the two players, and the way player ids are numbered makes collisions common. At 100 players, between 46% and 66% of pairs shared a hash with some other pair. Colliding pairs overwrite each other's record. Depending on the order, both pairs then send a location update every tick, about 23,000 extra packets per second at 100 players, or a player's first position is never sent and their dot never appears. The Windows build does not have this problem. [Filling in the Linux column](linux-fill.md) tells how the same count turned up during our Linux benchmark day.

Endstone keeps exactly one record per pair, so on Linux it now behaves like vanilla on Windows. Part of our Linux speed-up comes from no longer paying for that bug.

## The result

In the A/B at 100 spread players, the locator bar change took about 7 ms per tick off the main thread. It shipped in Endstone 0.11.14 as commit `874c1fa31`. It is one of the changes behind the drop from vanilla's 105 ms per tick to 43 ms on Windows in our benchmark.

For the release, separate chaos runs checked the locator bar together with the other changes we compare against vanilla. They found 0 differences in 745 million checks on Windows. On Linux they ran 833 million checks, and the only differences were the locator pairs explained above.

## What stayed with us

We expected the table to stay small, and one step on the lookup path made it hold every pair. We now read what a lookup does when it comes up empty before we trust any description of a table.

The comparison build logged about 24 million disagreeing checks for this change, all on Linux. Every one of them came from vanilla's merged pairs.
