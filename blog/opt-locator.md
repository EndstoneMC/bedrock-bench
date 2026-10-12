---
title: "9,900 pairs a tick: cutting the cost of the locator bar"
date: 2026-10-12
summary: "The locator bar's cost grew faster than the square of the player count, and checking our fix against vanilla turned up a bug in vanilla's Linux build."
tags: [performance, locator-bar, equivalence, linux, endstone-0.11.14]
---

The locator bar is one of the nicer recent additions to Minecraft. Each player sees a dot for every other player, placed along the top of the screen in that player's direction. On a small server it costs almost nothing. We wanted to know what it costs at 100 players.

To keep those dots right, the server considers every ordered pair of players, one viewer and one observed. With 100 players online that is 9,900 pairs, checked every tick. At the full 20 ticks per second that would be almost 200,000 pair decisions per second, and nearly all of them end with nothing to send.

## The symptom

We first noticed the locator bar while profiling vanilla BDS with bots spread across a world. At 60 players it cost 3.2 ms per tick on the main server thread. That was small next to the [autosave problem](chunk-storage.md) we were chasing at the time. Then we took a steady capture at 100 players and it cost 10.9 ms per tick.

Two thirds more players made it 3.4 times as expensive. Plain pairwise work would have made it 2.8 times. Something in there grew faster than the square of the player count.

## Splitting the profile

We took the sampling profile apart frame by frame. The locator bar time fell into separate piles: memory allocation, memory copying and comparing, looking up the entity behind each player, and the position math itself. The math was the floor. Everything else could, in principle, go.

Then we worked out how each part should grow with the number of players. One part grew with N² log N, one with N³ and one with N². We fit those models at 100 players and checked the prediction at 60. It landed within a few percent. Our picture of the code matched what was really running, so we could trust the model beyond 100 players. It put the locator bar at about 60 ms per tick at 200 players, more than the whole 50 ms budget of a tick.

## Two surprises

The first surprise was a table. On paper it held records only for pairs of players far enough apart to need special handling. Then we checked what happens when the server looks up a pair that is not there yet. It adds an empty record. So every pair ended up in the table regardless of distance. At 100 players that is 9,900 entries, searched over and over every tick.

The second was a short piece of text that the server copied for every pair on every tick, whether or not it had changed. It was too long to fit in the string's small built-in buffer, so each copy went to the heap. Comparing in place and copying only on change removed those allocations with no change in behaviour.

## Deciding what identical means

Endstone's rule for performance work is that gameplay must not change. Before we could claim that, we had to pin down what it meant here. Vanilla gives some objects random identifiers, and that makes the order of packets differ from one vanilla run to the next. We settled on identical up to that randomness. Our code still asks vanilla for those identifiers in the same order, so the random sequence lines up.

## The fix

The fix keeps vanilla's decision code. In front of it sits a cheap check that proves, for a given pair, that this tick's work cannot change any packet. When the proof succeeds, we skip that pair. When it does not, the pair goes through vanilla's own decision code, run against Endstone's own record for that pair. Any pair that might send something takes vanilla's path, so the result is exact by construction.

Before skipping anything, we listed everything vanilla's work touched along the way. The lookups it made were all read-only, so making them again on our side changed nothing. We also kept vanilla's distance math. Floating point results depend on the order of operations, so our check calls vanilla's own test instead of a copy of it.

We held back one tempting idea: sharing one player's data across all of that player's viewers within a tick. That is only exact if nothing between two viewers can change that player, and we had not shown that. It stays out for now.

## Checking it against vanilla

We first built the fix as a small experimental patch loaded into stock vanilla BDS, so the A/B compared the same server binary with and without it. Then we ported it into Endstone, along with an instrumented build that runs vanilla's code next to ours for every pair and compares the packets byte for byte.

We put that build through 15 minutes of chaos: joins, kicks, teleports, deaths, dimension changes, game mode switches, and the locator bar game rule flipped on and off repeatedly. On Windows the comparison covered 78 million pair checks and found zero differences.

On Linux, 98.5% of 101 million pair checks were proven to have nothing to send. About a quarter of the checks still disagreed with vanilla. In most of them, vanilla sent a location update where ours sent nothing.

Our first assumption was that the bug was ours. The table sizes said otherwise. With 23 players online there are 506 ordered pairs. Our table had 506 entries. Vanilla's had 504.

## A bug in vanilla's Linux build

The cause was in vanilla. The Linux build of BDS files its per-pair records under a weak hash of the two players, and the way player ids are numbered makes collisions common. At 100 players, between 46% and 66% of pairs shared a hash with some other pair. Colliding pairs overwrite each other's record. Depending on the order, both pairs then send a location update every tick, about 23,000 extra packets per second at 100 players, or a player's first position is never sent and their dot never appears. The Windows build does not have this problem. [Filling in the Linux column](linux-fill.md) tells how the same count turned up during our Linux benchmark day.

Endstone keeps exactly one record per pair, so on Linux it now behaves like vanilla on Windows. Part of our Linux speed-up comes from no longer paying for that bug.

## The result

In the A/B at 100 spread players, the locator bar change took about 7 ms per tick off the main thread. It shipped in Endstone 0.11.14 as commit `874c1fa31`. It is one of the changes behind the drop from vanilla's 105 ms per tick to 43 ms on Windows in our benchmark.

Across every instrumented check in that release, the comparison found 0 differences in 745 million checks on Windows. On Linux it ran 833 million checks, and the only differences were the locator pairs explained above.

## What stayed with us

The empty record is the part we keep coming back to. The table was meant to stay small, and one step on the lookup path made it hold every pair. We now read what a lookup does when it comes up empty before we trust any description of a table.

The comparison build logged about 24 million disagreeing checks in this change. Every one of them came from one bug in vanilla, and two missing table entries were enough to show it.
