---
title: "Two chunks a second"
date: 2026-10-12
summary: "The chunk cost that mattered at 100 players was a view check behind two caches that wiped themselves when full, and the fix was to raise their caps."
tags: [performance, chunks, profiling, client-side-generation, endstone-0.11.14]
---

Earlier in the 0.11.14 cycle we [fixed how the server picks chunks to autosave](chunk-storage.md), which had been taking 45% of the main thread on vanilla at 60 spread players. With that out of the way, chunk loading was next on our list. Our benchmark puts 100 players on a fresh world, each in a separate area, at view distance 10. New terrain is always arriving for someone. Every chunk that loads runs some setup work on the main thread, and we wanted to know its cost.

Getting that number right took longer than we expected. Then a second number, two chunks a second, turned the whole idea into a detour. The fix we shipped was somewhere else: two caches that threw away everything they knew whenever they filled up.

## A profile that changed its mind

We record the main thread with a sampling profiler and turn the samples into tables with our own scripts. [What the server was really simulating](block-sim.md) first spotted this per-chunk cost from the redstone side. The first table said the work that runs when a chunk finishes loading took 3.8% of the main thread. That looked worth chasing.

Then we rebuilt the table from the same recording and window, with one other run processed first. It read 0.9%. On its own, it read 0.2%.

The bug was ours. Our stack resolver cached its results by the memory identity of the loaded profile. Once the first profile was freed, Python could give the next one the same identity, and the second profile quietly picked up the first one's stacks. Any comparison table built from several runs in one process was wrong after its first column.

Every profile now gets its own process. The 0.2% was the real number.

## What one chunk costs

A sampling profiler gives shares of time, and we wanted the cost of a single call. We sorted the main thread's samples by time, kept the ones whose stack contained the function, and split them into runs wherever the gap was longer than about 1.2 sample periods. A histogram of run lengths showed peaks at k and 2k samples. The 2k peak is two chunks loading back to back, so one call costs k times the sample period.

The per-chunk setup step came out at about 0.75 ms. That made sense once we counted blocks. One load step visits every non-air block in the chunk, and a natural chunk has 35,000 to 45,000 blocks that are not air. At 20 ns a block, that comes to 0.75 ms.

Almost none of those blocks do anything in that step. Bedrock stores each sub-chunk as a palette: a short list of the distinct blocks in it, plus an index for each position. You can ask the palette once per distinct block whether anything needs work, and skip all 4,096 positions when nothing does.

A list of redstone blocks looked like the obvious filter. It would have been wrong. Two-block-tall plants such as sunflowers use the same step to clean up a top half left without its bottom half, and a redstone-only filter would silently switch that off. Our design asks whether a block does anything in this step at all, runs its own scan, and hands the rest of the step back to vanilla so every side effect happens in the same order.

## 100 chunks a second, or 2?

The bot logs showed about 100 chunks a second arriving during the steady window, and we assumed the server was loading about as many. At 0.75 ms each, that would have been 75 ms of main-thread time every second.

The server was loading about 2 chunks a second. Sending runs far behind loading, so in the steady window players were still receiving chunks that had loaded long before. We got the real rate by dividing the main-thread time spent in the load path by the 0.75 ms that one chunk costs.

At 2 chunks a second the block walk costs about 1.5 ms of main-thread time per second, well under 1% of the main thread. The idea was sound, and once players settled it would have saved almost nothing. The cost only mattered while all 100 players were joining. It did not ship.

## A cache that forgets everything

The corrected profile also showed 3.5% of the main thread going somewhere else. Our benchmark runs with client-side generation on, so the client builds fresh terrain from the world seed itself. The server still has to decide which chunks in each view it can leave to the client. In a later run at our target settings on BDS 1.26.51, with the autosave fix patched in, a tick took about 93 ms, and the check took 19.1 ms of it.

For each chunk in that part of the view, the server asks two questions. Does this chunk already exist in the world? Does its terrain need blending with older terrain next to it? Both answers are cached, so the check should have been cheap. Yet under it, the main thread sat in reads from the world database.

So we counted. Every time a player crosses a chunk border, the check runs again for every empty slot in that player's view and the chunks around it. In the steady window it ran about 700,000 times a second, and 1.6 million while the bots walked. Then we looked at what each cache does when it reaches its size limit. It wipes itself whole and starts again from empty.

With 100 players spread across the world, both caches filled and emptied every few seconds. Most lookups missed. Each miss sent the server back to work the answer out again, with reads from the world database on the main thread.

Our first fix (`a46a12cdb`) remembered the blending answers in a cache of our own. A prototype of that cache saved about 11 ms per tick in an A/B run at 100 players on 1.26.51. A second cache needs its own rules for when an answer goes stale, and vanilla's caches already had correct ones. The only thing wrong with them was the size limit.

So we dropped our cache and raised vanilla's caps instead (`2bf178a99`). At this benchmark's rate, a wipe now happens about once every 25 minutes. The server asks the same questions and gets the same answers, and every path vanilla uses to clear a stale answer is still there. Only timing changes. We did not measure the cap raise on its own. It shipped with the rest of 0.11.14.

## What we took away

Endstone 0.11.14 as a whole took our 100-player Windows benchmark from 9.40 TPS on unmodified vanilla BDS 1.26.52 to 20.0, and the main thread from 105 ms to 43 ms per tick. The raised cache caps are one of the changes behind that.

The lesson that stuck hardest was about our own tools. One recording gave one function three different shares, and we only noticed because the numbers refused to agree. The other lesson is to look for size limits early. A cache that wipes itself whole when it fills misses on most lookups once the working set outgrows it, as these two did.
