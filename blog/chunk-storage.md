---
title: "The autosave picker that slowed down with every chunk"
date: 2026-10-12
summary: "A random pick in Bedrock's autosave got slower with every chunk loaded, and fixing it kept 60 spread-out players at 20 TPS where vanilla stayed under 14."
tags: [performance, chunks, autosave, bds, endstone-0.11.14]
---

On October 10 we spread 60 headless players across a fresh world on a vanilla Bedrock Dedicated Server 1.26.51. Each one walked around its own patch of land, 640 blocks from its nearest neighbour. The server dropped to about 11 TPS and stayed there. The main thread was spending around 85 ms on every tick, and a tick only gets 50.

## The usual suspects

Our first guess was terrain. Spread players need a lot of chunks, and every one of them has to be generated, lit and sent.

We checked whether the main thread waited on a lock in the code that starts chunk generation. It tries that lock once. If another thread holds it, the main thread skips that work and moves on.

Saving was the other suspect, since a world growing this fast has a lot to write. We checked whether each save rewrote the whole chunk. The server skips every part of a chunk that has not changed since its last save and writes only the changed pieces.

Then we ran a sampling profiler. About 45% of the main thread went to the autosave picker, the code that chooses which loaded chunk the autosave checks next. While the players were joining, the share was 57%.

## How the picker works

Besides the periodic saves, the server runs an autosave sampler on every tick. It picks loaded chunks at random, up to 20 picks per tick, and stops as soon as it has saved one chunk that changed.

To choose a random chunk, it draws a random number k and walks k steps from the start of a linked list of every loaded chunk. With players gathered at spawn that list is short and nobody would notice.

With 60 players spread out, the list held tens of thousands of chunks. A single pick could walk tens of thousands of steps before it reached its chunk. In a tick that needed all 20 picks, that came to hundreds of thousands of steps on the main thread, and the sampler ran again on the next tick.

A test with everyone standing near spawn never shows this. Every benchmark we run now spreads the players out.

## The fix

The fix keeps a snapshot of the loaded chunks in a flat array and looks up the drawn index directly, so each pick costs the same no matter how many chunks are loaded. The snapshot is rebuilt every 400 picks. At our heaviest load a rebuild takes about 16 ms. The sampler makes at most 20 picks per tick, so a rebuild comes at most about once a second, which averages out to under 1 ms per tick.

The random draw is made exactly as vanilla makes it. The snapshot can be up to 400 picks old, so a pick can land on a different chunk than vanilla's would. The sampler already picked at random, so the change only affects when a given chunk gets saved. That made this a timing-only change.

## What it did

We applied the fix to a vanilla 1.26.51 server and ran it against an unmodified one, with the same 60 spread players and the same seed in both runs.

During the join, TPS went from 12.7 to 19.9. Main thread time fell from 870 ms to 459 ms of CPU per second.

After the join, the unmodified server sat between 9.6 and 13.7 TPS. With the fix it held 20.0 at every sample we took, up to 350 seconds after the join, and it had more chunks loaded than the unmodified run.

We then ported the same change to Endstone, and it shipped in 0.11.14 for both Windows and Linux. In the changelog it is one line: "Improved server performance when many chunks are loaded, such as when players are spread out across the world."

## What came next

This was the first of several fixes. With the picker out of the way, random block ticks became the largest cost at about a third of the main thread, and redstone came next even in a world with no redstone in it. Removing 45% of the main thread had reordered everything below it, so we profiled again before touching anything else.

Those fixes are stories for other posts, starting with [22 ms to find 0.73 ms of work](opt-random-ticks.md). [What the server was really simulating](block-sim.md) tells this same autosave story from the block simulation side. Together they brought Endstone 0.11.14 to 20 TPS with 100 spread players. That benchmark used a lighter setup, view distance 10 and simulation distance 4 on 1.26.52, and on the same machine with the same settings vanilla managed 9.4.
