---
title: "44,700 spawn chunks: the teleport stall we blamed on the wrong code"
date: 2026-10-12
summary: "Our teleported bots received 46,000 chunks and kept hovering, because almost all of them came from the spawn area they had just left."
tags: [performance, chunks, teleport, client-side-generation, benchmarks, endstone]
---

Our benchmark puts 100 headless players on a fresh world and teleports each one to its own cell of a grid, 640 blocks apart, at height 300. They hover there until the ground under them loads, then fall. In one run, a bot received no terrain at all for three minutes after its teleport. Others received hundreds of chunks and kept hovering anyway. We wanted to know why.

The bot logs split into tiers that mostly followed join order. The first 14 bots received some terrain around themselves. The next 37 had 230 to 500 chunks each and were still hovering. A last group of 47, mostly the latest to join, had 600 to 800 each. The remaining two bots fit no tier. One of them, a74, comes up below.

Our harness had a workaround: any bot still hovering with fewer than 30 chunks after a minute got teleported 16 blocks sideways. The bots seemed to recover, so the nudge looked like a cure.

## A tidy theory

The server keeps a view for each player: a grid of chunk slots around them. When a slot cannot be filled, it stays empty. The server only looks at empty slots again when the grid moves, and the grid only moves when the player crosses a chunk border. A hovering bot never crosses one. A 16-block nudge always crosses exactly one.

We started calling it the "view retry" problem and planned a small hook to revisit empty slots for players who stand still.

The mechanism is real. Then we checked whether it fired. In the tiered run our bots built no terrain themselves, and for a player like that the check that decides whether to fill a slot always passes. No slot around a bot should have been left empty, so a retry had nothing to pick up.

So we went back to the nudge run, a separate run with view distance 10 and bots that build their own outer terrain. The harness picked bots from status lines printed once a minute, so it acted on counts up to a minute old. Their 10-second totals show the nudged bots had already climbed from 842 chunks to about 3,500 before the first nudge, at about 65 chunks per second. After the nudges the rate was about 71. The recovery was already under way.

## Where the chunks came from

That left the bigger puzzle: in the tiered run, the bots received about 46,000 chunks after their teleports and stayed in the air.

The main thread kept building chunk packets until 68 seconds after the first join, but after about 15 seconds the profile showed no lighting work and no newly finished chunks, so those sends came from an older backlog.

Next we bounded what generation could have produced while those sends were going out: at most about 50,000 tasks. Each finished chunk needs several steps plus work on its neighbours, so that is fewer than 10,000 chunks. One player's area takes about 6,000 tasks, and the 47 late bots alone would have needed about 280,000.

The landings agreed. We believe a hovering bot only falls once the server simulates it, which needs the chunk under it. If a bot's own ground had been among the chunks it received, bots with more chunks should have landed sooner. They did not.

Only the spawn area and the first few bots' cells had finished terrain that early. After landing, each late bot received 800 to 1,100 chunks at positions it had not seen before, so its earlier chunks were not from its own area. The two hovering tiers account for about 44,700 of the 46,000, and we concluded nearly all of those were spawn chunks.

## The pin

Each player has a second view, the one that decides which chunks get sent. On a world with client-side generation enabled, which is the vanilla default, the server will not re-centre that view while it is still streaming chunks to the player. A chunk sent in the last quarter of a second holds it in place. Generation does follow the player, so it moved to the teleport target while delivery stayed at spawn.

Most of our bots were teleported between 0.3 and 2 seconds after spawning, while the spawn area was still streaming to them. The late bots drained 600 to 800 spawn chunks each, up to about 40 seconds of sending. Only then did their delivery view move, to an area where nothing had finished yet. The middle tier drained part of the backlog until a short gap in their stream let the view move. The first 14 teleported before the spawn stream got going, so their views moved at once.

Two bots gave us a free A/B test. a74 and a75 joined in the same millisecond. a74 was teleported 0.43 seconds after spawning, before its stream had resumed, so its view moved at once, to an area with nothing finished. It received nothing for three minutes. a75 was teleported 0.86 seconds after spawning, got pinned, and received 738 spawn chunks. As far as we can tell, only the teleport delay differed.

The same quiet-period rule decides when a joining player leaves the loading screen. Our bots got through it by accident. They did not acknowledge cached chunk transfers early, so the server paused after about a hundred chunks, and that pause let them spawn. A real client that acknowledges promptly has to receive every finished server-built chunk in its view first.

## What it means for players

Some of the tiers came from our harness, which teleports each bot within two seconds of spawning. The rule itself is vanilla. With client-side generation on, a player who uses `/tp`, a warp or `/spawn` while their chunk stream is still busy, such as soon after joining, should keep receiving the area they just left until that backlog runs out.

We have not measured this with real clients. Over RakNet, from about 20 players up, each player gets one chunk per tick, so every 100 chunks of backlog is 5 seconds of nothing new at 20 TPS. A client that builds its outer terrain itself waits only for the server-built part of its view, so its backlog should be smaller than our bots' 600 to 800.

We designed a fix that re-centres the view right away on teleport-sized jumps, and always for clients that do not generate chunks themselves. Vanilla's rules still decide where the view goes. Only the wait is skipped. We have not built or A/B tested it yet, so it is not in Endstone 0.11.14.

One related change did ship in commit `099d6f028`, described in [Parking the chunks that are not ready](opt-publisher.md): queued chunks that are outside the player's view or not finished yet are set aside instead of being re-checked every tick. That covers the spawn chunks a teleported player leaves behind in its queue. It does not shorten the wait.

The retry gap is still there. We found no run where it mattered.

Our harness should count only chunks near each bot's current position. A per-bot total mixes spawn chunks with the bot's own area.
