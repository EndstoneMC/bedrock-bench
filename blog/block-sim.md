---
title: "What the server was really simulating"
date: 2026-10-12
summary: "We set out to measure what block simulation costs on a busy Bedrock server, and the biggest entry in the profile turned out to be the autosave."
tags: [performance, profiling, random-ticks, redstone, autosave, endstone-0.11.14]
---

In early October we put 60 bots on a vanilla Bedrock Dedicated Server 1.26.51 running on Windows. We spread them across a fresh world so that no two shared a chunk, and let them walk. The server settled at about 11 TPS. The main thread spent around 85 ms on every tick. At 20 TPS the budget is 50.

Our working theory was simple. Spread-out players load a lot of world, and a lot of loaded world means a lot of blocks to simulate. Random ticks grow crops and spread grass in the chunks around each player, and the server runs separate passes for redstone and for block entities such as furnaces. So we set out to understand block simulation properly, one loop at a time, and to write down exactly what each loop visits.

## The ticking area was smaller than we thought

We had assumed that a simulation distance of 4 meant a 9 by 9 square of ticking chunks around each player, 81 in all. When we worked out the bounds of the loop the server actually runs, the area was a rounded shape of 57 chunks. That is 30% fewer than our back-of-envelope model.

From then on, whenever we asked which chunks a loop touches, we worked it out from the loop itself. Our reading of the settings had already been wrong once.

## Redstone in a world with no redstone builds

The redstone pass took 7.8% of the main thread in a fresh natural world where nobody had built a single circuit. Most of that time went into checking whether chunks were fully loaded.

Reading the loop that evaluates circuits did not explain it. The answer came from asking how chunks get into that loop in the first place. Every chunk signs up with the redstone system when it loads, whether or not it holds any redstone. So the redstone pass spans the whole loaded area, which with spread players is far larger than the ticking area. The sign-up itself costs about 0.75 ms of main-thread time for each chunk that loads. [Two chunks a second](opt-chunk-load.md) follows that cost further.

Block entities held the same surprise. The server keeps a list of block entities to tick, and we assumed it held only the furnaces and hoppers with something to do. The check that decides who goes on the list says yes to every block entity.

## Thread safety for one thread

Inside the per-block code we kept finding small guards. One was a check of which thread is calling, on a slower path that some per-block queries fall back to. Another was a lock-style write to the random number generator's state, which only one thread ever touches. Each costs little on its own. Random ticks pay that write once per pick, and they make many picks in every ticking chunk on every tick. We started treating any lock or thread check inside a per-block loop as worth a look.

## The entry that did not belong

The top entry in our profile was a function that fetches a random loaded chunk. It held 45% of the main thread at 60 players. We had filed it under world simulation. Random ticks pick random positions, and a random chunk seemed to fit.

Then we asked what calls it on every tick, and the answer was the autosave. Bedrock saves the world gradually. On every tick, an incremental saver on the main thread picks loaded chunks at random, up to 20 picks, and stops once it has saved one chunk that changed.

The cost was in the pick. The loaded chunks live in a lookup table whose entries are chained together in a list. A table like that has no way to jump straight to the nth entry, so to reach a random position the picker walked the list from the start. With players spread out, the list held tens of thousands of chunks, and the picker could walk it 20 times in one tick.

The cost grew with every chunk loaded, and every player walking onto fresh ground added more. Block simulation had nothing to do with it. Since then, for every hot function we ask what calls it on every tick before deciding which part of the server owns the cost. We also treat any helper that picks a random entry from a table like that as suspect.

## An array instead of a walk

The fix is small. Endstone keeps a snapshot of the loaded chunk positions in a plain array and indexes into it directly. The snapshot is rebuilt every 400 picks. The random number is drawn exactly as vanilla draws it, so the shared random sequence the rest of the world relies on stays in step. The only thing that can differ is which chunk the autosave writes next. That is a timing change to disk writes, and nothing a player sees changes.

We tried it first as a small patch on vanilla BDS, with the same seed and the same 60 spread bots. During the join, TPS went from 12.7 to 19.9, and main-thread work dropped from 870 ms to 459 ms per second. Afterwards, vanilla wobbled between 9.6 and 13.7 TPS. The patched server held 20.0 at every sample up to 350 seconds after the join, and it had more chunks loaded than vanilla managed in the same time.

No other single item in that profile came close to the picker's 45%. The change shipped in Endstone 0.11.14 as 6f0c32205 on Windows and bec9a2fd7 on Linux.

## Block simulation, at last

With the autosave out of the way, the profile finally showed block simulation on its own. At 60 spread players the main thread dropped to 47 to 49 ms per tick. Random ticks were now the largest item at 16.4 ms per tick, a third of the total. The redstone pass came next at 7.0 ms per tick, still in a world with no redstone builds.

## What came next

Random ticks and the redstone pass became our next targets, and random ticks got cheaper in 0.11.14 as well ([22 ms to find 0.73 ms of work](opt-random-ticks.md)). The autosave fix is told in more detail in [The autosave picker that slowed down with every chunk](chunk-storage.md). Our release benchmark puts 100 spread players on a slightly newer BDS, 1.26.52. On Windows, vanilla holds 9.40 TPS with 105 ms of main-thread time per tick. Endstone 0.11.14, with the picker fix and the rest of that release, holds 20.0 TPS at 43 ms. On Linux the same benchmark goes from 9.46 TPS to 20.0.
