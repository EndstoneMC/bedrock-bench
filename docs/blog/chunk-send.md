---
title: "Where the chunks wait"
date: 2026-10-12
summary: "We traced a chunk from the generation workers to the player, ranked the wrong cost first, and a profile showed the main thread re-checking unfinished chunks for every player on every tick."
tags: [performance, chunks, networking, bds, endstone-0.11.14]
---

On 10 October we were profiling a vanilla Bedrock Dedicated Server 1.26.51 with 60 headless players spread across a fresh world, so that no two shared a chunk. We had just fixed [the autosave picker](chunk-storage.md), which had been the biggest cost on the main thread. With it gone, 60 players held 20 TPS once they had settled. The join wave was another matter. While players waited for their terrain, sending chunks to players took 41% of the main thread, about 9.6 ms per tick.

Earlier that day we had already traced a chunk's path from the generation workers to a player. That trace was where we started.

## How a chunk gets to a player

Every player has a send queue. When a chunk enters their view, it goes into that queue, usually well before it has finished generating. Once per tick, for every player, the server copies the whole queue into a fresh list, sorts it nearest first, and walks it. Each entry gets one question: is this chunk loaded and ready? Ready chunks go out. The rest stay queued and the walk moves on.

The first surprise was the send budget. The server has a small budget of chunks per tick, divided by the number of players online, with a floor of one. With a few dozen players connected, every player gets one chunk per tick, twenty a second, however empty their screen is.

## A wrong turn

Our first suspect was the step that builds the chunk packet. It looked like where terrain was serialised per player, the natural spot for a cache. By default that step only handles biomes. For terrain, the client asks for the sub-chunks it needs, and the server answers each request by serialising those sub-chunks for that player. If the client has seen the terrain before, the blob cache lets the server send a hash in place of the bytes.

The server-wide blob store sounds like a shared terrain cache. It holds each blob only until every client that got it has acknowledged it. Each player's terrain is still serialised and hashed before the store is consulted, so all it saves is a duplicate copy in memory while the bytes are in flight.

Our cost model from tracing the code ranked the sort first, since copying and sorting every queue on every tick with that tiny budget adds up to quadratic work during a join.

Further down the list sat some thrown-away work. While a chunk is being prepared, the server does some work for every sub-chunk and throws most of it away. A little later the main thread does similar work again, once for every player who asks. That looked like a cache waiting to be built.

## What the profile said

With 60 players holding 20 TPS, we moved up to the load we want to serve: 100 spread bots at view distance 10 with client-side chunk generation on. Vanilla managed 10.75 TPS there. We took a new profile once every player had joined and chunk delivery had settled.

The profile disagreed with our cost model. The per-player send loop took 11.5 ms per tick. The sort we had worried about was 1.3 ms of that. The biggest share, 4.2 ms, was the per-entry send attempt, and most of it went to finding entries that were not ready. Another 1.9 ms went to blob-cache bookkeeping that was set up and torn down for each attempt, sent or not.

Even after the joins settled, many players' queues were still full of unfinished chunks. Every tick, for every player, the loop tried them all in order, found each one not ready, and sent nothing.

A second cost sat inside it. Once per player per tick, the server refreshed the list that tells the generation workers which areas matter most. Each refresh copied every player's entry, then waited on a lock the generation workers held. In the settled profile it was cheap, 0.24 ms per tick. In a separate profile taken while chunk delivery stalled during the join wave, the send loop took 28 ms per tick and the refresh took 4 ms.

## The fix

Chunks that cannot be sent yet now wait in a separate place the loop never looks at. We call this parking. A parked chunk goes back into the queue when it could have become sendable, which means it finished loading or the player's view moved. A slow periodic sweep catches the rare cases those two miss. Vanilla's own loop then runs on a handful of entries, sorts them nearest first and sends them exactly as before.

Each player still gets its chunks nearest first. Chunks at exactly the same distance can swap places, because vanilla's sort makes no promise about ties.

The generation priority refresh now runs once per tick per dimension instead of once per player. That half is a timing change. With client-side generation on, a player's view move can start generation work partway through the player loop. That work now sees the previous tick's priorities, which can change the generation order for at most one tick.

## The result

We first measured the fix as a prototype on vanilla 1.26.51, alongside three other prototypes. In the 100-player A/B test, with both sides profiled after the players had settled, the send loop dropped from 11.5 ms per tick to 0.35 ms. It shipped in Endstone 0.11.14 as commit 099d6f028. [Parking the chunks that are not ready](opt-publisher.md) has the details of how parking stays exact.

We also checked that nothing else moved. During a long chaos run of joins, kicks, teleports and dimension changes, an instrumented build compared vanilla's code with ours on every call for chunk publishing, player lookup and the locator bar: 745 million checks on Windows and 833 million on Linux. Parking never held back a chunk vanilla would have sent. Tie order did change: on Linux, 149,125 of 371,264 checked sends went out in a different order from vanilla, all among chunks at exactly the same distance. The locator bar differed on Linux only because of a vanilla Linux bug that merges some pairs of players.

In the 0.11.14 release benchmark, the median time for a player to hold 90% of its view went from 192 seconds on vanilla 1.26.52 to 81 seconds on Windows, and from 194 to 130 seconds on Linux. Those figures include every change in the release, and this one mainly cuts main-thread time per tick. We believe most of the long waits at 100 players come from somewhere else, a rule that holds a player's chunk stream in place, which [another post](opt-view-retry.md) covers.

## What we took away

The thrown-away sub-chunk work is still on our list. Keeping it could spare the main thread some of its per-player work. We have not built that yet, since the profile pointed elsewhere first.

Tracing the code gave us about ten candidate costs. The sort we had ranked first turned out to be about a tenth of the loop. Re-checking unfinished chunks, which drove the largest share, was not on the list at all.
