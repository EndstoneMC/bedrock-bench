---
title: "Following one movement update through the server"
date: 2026-10-12
summary: "We traced how a movement update reaches the players who can see it, predicted about 20 movement updates per second from each player in sight, and found our load test had measured about 28 packets per second per bot of every kind."
tags: [performance, bds, networking, benchmarks, endstone]
---

Every time a player or a mob moves, the server has to tell everyone who can see it. With 100 players online, this is one of the places where cost can grow with the square of the player count. Before trying to make it cheaper, we wanted a model of it on paper. We had two questions: who receives each update, and how many times each one is encoded.

## Following one update

We traced one update from where it is created to where it leaves the server, and counted the work at each step.

That last step turned out to matter most. Whether an update is encoded once and shared, or encoded again for each viewer, is decided there, whatever kind of update it is.

Sharing the encoded bytes is only safe when every recipient would get the same bytes. We checked what each protocol version actually puts on the wire. Recent versions added a field to mob movement updates, and we could not rule out that it differs from one recipient to the next. Until we can, those updates cannot be assumed safe to encode once and copy.

Player movement, which is what our estimate below counts, leaves by a different route. The path that sends mob movement does nothing for players, so we traced the player path separately.

We also looked at what each relevance check walks over. The code that visits the players in one dimension walks every player on the server and skips the ones elsewhere. A mob in the Nether still steps through every player in the Overworld. Every check built on it gets slower with each player who joins, wherever that player is. The same loop stood out in our profiles at 100 spread players, and the cached player list that replaced it in Endstone 0.11.14 has [its own post](opt-player-scans.md).

## A bug that was not there

Our first big finding was false.

One routine works out which players should hear about each entity. As far as we could tell, it built that list and then never used it. On every tick, for every entity, the server would be doing the work and throwing the answer away. That would have been a serious bug in a shipping server.

The mistake was ours. We had only looked at the first part of a long routine. When we read it to the end, the second phase was there, and it used the list the way you would expect.

## The estimate

With the path mapped, we could predict traffic. By our reading, every tick each player's input produces one movement update for every player who can see them, whether or not the player moved. At 20 ticks per second, a viewer should receive about 20 updates per second from each player in sight.

We had a measurement to compare against. An earlier run of our load harness, with its bots placed close together, had recorded about 28 packets per second per bot, counting every kind of packet together. By our estimate, a bot with only two others in sight should already receive 40 movement updates per second from them. A bot with 29 others in sight should receive 580, about 20 times the measured total for every kind of packet combined.

A gap that size left two explanations. Either the bots could not see each other, or our reading had missed something again. After the routine we had misread, the second one did not feel far-fetched. We could not tell them apart from the code, so we wrote the gap down as unexplained, with both explanations next to it, and did not average it away.

## A suspect yardstick

Separately, while profiling vanilla, we found that our load bots had never moved. They never told the server their loading screen had closed, so the server held them in place and ignored their movement input for the whole run. That story is in [Our bots had never moved](profile-vanilla.md), along with how far the fixed bots moved our vanilla capacity numbers.

The bots behind the 28 figure came from that old harness. Standing still would not explain the gap on its own, because by our reading a player who stands still keeps producing a movement update every tick. Having its input ignored might. If input is what triggers the update, those bots would have produced few movement updates or none, whether or not they could see each other.

That makes the frozen bots our leading suspect for the gap. We will not call it settled until we measure walking bots that have finished loading and stand close together.

## The next run

Our estimate gives the next run a clear target. Thirty walking bots in one cluster should each receive 29 × 20 = 580 movement updates per second. If they come close, our map of the path holds. If they come in near 28 again, we missed something a second time, and the code is where we look next.
