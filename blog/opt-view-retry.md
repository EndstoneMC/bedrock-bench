---
title: "44,700 spawn chunks: the teleport stall we blamed on the wrong code"
date: 2026-10-12
summary: "Our teleported bots received 46,000 chunks and kept hovering, because almost all of them came from the spawn area they had just left."
tags: [performance, chunks, teleport, client-side-generation, benchmarks, endstone]
---

Our benchmark puts 100 headless players on a fresh world and teleports each one to its own cell of a grid, 640 blocks apart, at height 300. They hover there until the ground under them loads, then fall. In one run, a bot received no terrain at all for three minutes after its teleport. Others received hundreds of chunks and kept hovering anyway. We wanted to know why.

The bot logs split into tiers by join order. The first fourteen bots received some terrain around themselves. The next forty or so had 230 to 500 chunks each and were still hovering, and the last forty-seven had about 800 each.

Our harness had a workaround: any bot still hovering with fewer than 30 chunks after a minute got teleported 16 blocks sideways. The bots seemed to recover, so the nudge looked like a cure.

## A tidy theory

The server keeps a view for each player: a grid of chunk slots around them. When a slot cannot be filled, it stays empty. We found that the server only looks at empty slots again when the grid moves, and the grid only moves when the player crosses a chunk border. A hovering bot never crosses one. A 16-block nudge always crosses exactly one.

It fit so well that we named the investigation view retry and planned a small hook to revisit empty slots for players who stand still.

The mechanism is real. Then we checked whether it fired. In the tiered run our bots built no terrain themselves, so the server had to supply their whole view, and for a player like that the check that decides whether to fill a slot always passes. No slot around a bot should have been left empty, so a retry had nothing to pick up.

So we went back to the nudge run. The harness picked bots from status lines printed once a minute, so it acted on counts up to a minute old. The bots' 10-second totals show that the nudged bots had already climbed from 842 chunks to about 3,500 before the first nudge, at about 65 chunks per second. After the nudges the rate was about 71. The recovery was already under way.

## Where the chunks came from

That left the bigger puzzle: in the tiered run, the bots received about 46,000 chunks after their teleports and stayed in the air.

The profile was the first clue. The main thread kept building chunk packets until 68 seconds after the first join, but after the 25-second mark the profile showed no lighting work and no newly finished chunks, so those sends came from an older backlog.

Next we bounded what generation could have produced: at most about 50,000 tasks in that window. Each finished chunk takes several generation steps plus work on its neighbours, so that comes to fewer than 10,000 finished chunks. One player's area takes about 6,000 tasks, and the 47 late bots alone would have needed about 280,000.

The landings agreed. We believe a hovering bot only falls once the server simulates it, which needs the chunk under it, yet landing times did not depend on how many chunks a bot had received.

Then we sorted the post-teleport chunks by position: near the world spawn, or inside the bot's own cell. About 44,700 of the 46,000 were spawn chunks. The spawn area was the only terrain finished that early, and the bots kept receiving it long after they had left.

## The pin

Each player has a second view, the one that decides which chunks get sent. On a world with client-side generation enabled, which is the vanilla default, the server will not re-centre that view while it is still streaming chunks to the player. A chunk sent in the last quarter of a second holds it in place. Generation does follow the player, so it moved to the teleport target while delivery stayed at spawn.

Our bots were teleported between 0.3 and 1 second after spawning, while the spawn area was still streaming to them. The late bots drained about 800 spawn chunks each, about 40 seconds of sending. Only then did their delivery view move, to an area where nothing had finished yet. The middle tier drained part of the backlog until a short gap in their stream let the view move. The first fourteen teleported before the spawn stream got going, so their views moved at once.

Two bots gave us a free A/B test. a74 and a75 joined in the same millisecond. a74 was teleported 0.43 seconds after spawning, before its stream had resumed, so its view moved straight away. Its area had nothing finished, so it received nothing for three minutes. a75 was teleported 0.86 seconds after spawning, got pinned, and received 738 spawn chunks. As far as we can tell, only the teleport delay differed.

The same quiet-period rule decides when a joining player leaves the loading screen. Our bots got through it by accident. They did not acknowledge cached chunk transfers early, so the server held further chunks back after about a hundred, and that pause let them spawn. A real client that acknowledges promptly has to receive every server-built chunk in its view first.

## What it means for players

Some of the tiers came from our harness, which teleports each bot within a second of spawning. The rule itself is vanilla. With client-side generation on, a player who uses `/tp`, a warp or `/spawn` while their chunk stream is still busy, for example soon after joining, should keep receiving the area they just left until that backlog runs out.

We have not measured this with real clients. From about 20 players up, each player gets one chunk per tick, so every 100 chunks of backlog is 5 seconds of nothing new at 20 TPS. A client that builds its outer terrain itself waits only for the server-built part of its view, so its backlog should be smaller than our bots' 800. The server also spends compression time and bandwidth on terrain behind them.

We designed a fix that re-centres the view right away on teleport-sized jumps, and always for clients that do not generate chunks themselves. Vanilla's rules still decide where the view goes and only the wait is skipped, so it is a timing change. We have not yet run the A/B test for it on its own, so we left it out of Endstone 0.11.14.

One related change did ship in commit `099d6f028`, described in [Parking the chunks that are not ready](opt-publisher.md): queued chunks that cannot be sent yet are set aside instead of being re-checked every tick. That covers the spawn chunks a teleported player leaves behind in its queue. It does not shorten the wait.

The retry gap is still there. Nothing in our runs triggered it.

Looking back, the 46,000 total misled us longest, until we split it by where each chunk sat. What settled it was a74 and a75. Two bots from the same millisecond that end up that far apart point to a rule that depends on timing, and we now look for pairs like that early.
