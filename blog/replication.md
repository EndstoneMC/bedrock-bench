---
title: "Following one movement update through the server"
date: 2026-10-12
summary: "We traced how a movement update reaches the players who can see it, predicted about 580 of them per second for each of our bots, and found our load test had measured 27.6 packets per second of every kind."
tags: [performance, bds, networking, benchmarks, endstone]
---

Every time a player or a mob moves, the server has to tell everyone who can see it. With 100 players online, this is one of the places where cost can grow with the square of the player count. Before trying to make it cheaper, we wanted a model of it on paper. We had two questions: who receives each update, and how many times each one is encoded.

## Following one update

We traced one update from start to finish and counted the work at each step. We started where the update is created and stopped where it leaves the server.

That last step turned out to matter most. Whether an update is encoded once and shared, or encoded again for each viewer, is decided there, whatever kind of update it is.

Sharing the encoded bytes is only safe when every recipient would get the same bytes. We checked what each protocol version actually puts on the wire. Mob movement updates carry one small counter that differs from recipient to recipient, so they cannot simply be encoded once and copied.

The path that sends mob movement does nothing for players. Their movement goes out through a separate part of the server, which we had to find on its own.

We also looked at what each relevance check walks over. The helper that visits the players in one dimension walks every player on the server and skips the ones elsewhere. A mob in the Nether still steps through every player in the Overworld. That one detail changes how every caller scales with the player count. The same loop stood out in our profiles at 100 spread players, and the cached player list that replaced it in Endstone 0.11.14 has [its own post](opt-player-scans.md).

## A bug that was not there

Our first big finding was false.

One routine works out which players should hear about each entity. As far as we could tell, it built that list and then never used it. On every tick, for every entity, the server would be doing the work and throwing the answer away. That would have been a remarkable bug in a shipping server.

The mistake was ours. We had only looked at the first part of a long routine. When we read it to the end, the second phase was there, and it used the list the way you would expect.

## The estimate

With the path mapped, we could predict traffic. Every tick, each player's input produces one movement update for every player who can see them. The server sends it whether or not the player moved. At 20 ticks per second, a viewer receives about 20 updates per second from each player in sight.

We had a measurement to compare against. An earlier run of our load harness had put 30 bots in one cluster and recorded 828 packets per second, or 27.6 per bot, counting every kind of packet together. If those 30 bots could see each other, each one should have received 29 × 20 = 580 movement updates per second from the others alone. The prediction for one kind of packet was about 21 times the measured total.

A gap that size left two explanations. Either the bots could not see each other, or our reading had missed something again. After the routine we had misread, the second one did not feel far-fetched. We could not tell them apart from the code, so we wrote the gap down as unexplained, with both explanations next to it, and did not average it away.

## A suspect yardstick

Separately, while profiling vanilla, we found that our load bots had never moved. They never told the server their loading screen had closed, so the server skipped their movement processing for the whole run. That story is in [Our bots had never moved](profile-vanilla.md), along with how far the fixed bots moved our vanilla capacity numbers.

The 30 clustered bots behind the 27.6 figure came from that old harness. Standing still would not explain the gap on its own, because a loaded player who stands still keeps producing movement updates every tick. Being stuck on the loading screen might. From the code, players who have not finished their initial load appear to be left out of each other's view. If that applies to our bots, 30 of them could stand in one spot and never see each other.

That makes the frozen bots our leading suspect for the first explanation. We will not call it settled until we measure walking bots that have finished loading and stand close together.

## Testing the yardstick

We set out to model a cost and ended up doubting the instrument we had planned to check it with. The most useful result of the study was a disagreement. A prediction read from code told us our yardstick might be broken, and a separate finding gave us a reason to believe it.

The next run has a clear target. Thirty walking bots in one cluster should each receive about 580 movement updates per second. If they come close, our map of the path holds. If they come in near 28 again, we missed something a second time, and the code is where we look next.
