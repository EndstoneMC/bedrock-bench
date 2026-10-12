# Making BDS faster without changing gameplay

This series covers how we made Bedrock Dedicated Server faster without changing gameplay, and how we measured it.

Our benchmark puts 100 players on a fresh world, each in their own part of the map so that no two share a chunk, at view distance 10 and simulation distance 4. On that load, vanilla BDS 1.26.52 holds 9.40 TPS on Windows and spends 105 ms of main thread time on every tick. Endstone 0.11.14 on the same BDS build holds 20.0 TPS at 43 ms. On Linux it goes from 9.46 to 20.0 TPS. The median player now waits 81 seconds for their view to load on Windows, against 192 seconds on vanilla.

Every change in 0.11.14 had to keep gameplay identical. Some changes only affect timing, such as when a chunk is saved or which worker builds it. Ideas that would change the game, even slightly, stayed out. The posts below cover the measurements, the dead ends and the fixes.

## Suggested reading order

If you read only a few, read these in order:

1. [Our bots had never moved](profile-vanilla.md)
2. [The autosave picker that slowed down with every chunk](chunk-storage.md)
3. [What does one more player cost?](player-cost.md)
4. [Walking the player list 2,500 times a tick](opt-player-scans.md)
5. [9,900 pairs a tick](opt-locator.md)
6. [22 ms to find 0.73 ms of work](opt-random-ticks.md)
7. [Parking the chunks that are not ready](opt-publisher.md)
8. [Seven terrain workers, and most of them asleep](client-vs-server.md)
9. [Filling in the Linux column](linux-fill.md)
10. [Putting four other Bedrock servers through our 100-bot test](custom-bench.md)

The rest fill in the detail and can be read in any order.

## Measuring

- [Our bots had never moved](profile-vanilla.md): every load test we ran before 10 October used bots that never moved, and fixing that changed how we measure everything.
- [Filling in the Linux column](linux-fill.md): a day of harness fixes for the Linux benchmark, and a vanilla locator bar bug found by our check against vanilla.
- [Putting four other Bedrock servers through our 100-bot test](custom-bench.md): PocketMine-MP, Nukkit, PowerNukkitX and Dragonfly on the same load, and what each of them actually simulates.

## Finding the bottlenecks

- [We went looking for a compiler problem](codegen.md): how BDS is built, and the smaller fixed costs it pays on every tick, from security checks to memory allocation.
- [What the server was really simulating](block-sim.md): we set out to measure block simulation, and the biggest entry in the profile turned out to be the autosave.
- [Drawing a map of the Bedrock tick](tick-threading.md): which thread does what on every tick, and why game logic stayed on the main thread.
- [What decides whether a cow ticks](actor-tick.md): which mobs tick, what an idle mob does each tick, and the levers we left alone.
- [What does one more player cost?](player-cost.md): counting how per-player loops grow, pricing them with small replica programs, and checking the model against a profiler.
- [We thought the mobs would break it](light-spawn.md): our paper model put despawn checks at up to 15 ms per tick, and the profiler measured about 0.7 ms.
- [Every mob was asking about every player](hotpath-overheads.md): per-player work hidden in helpers that look like one cheap call, and a cost our old mob-free benchmark multiplied by zero.
- [Who is the nearest player?](ai-nav-collision.md): we went looking for the cost of pathfinding and collision and found one question that 22 places in the server keep asking.

## The fixes

- [The autosave picker that slowed down with every chunk](chunk-storage.md): a random pick that walked a list of every loaded chunk, and the fix that took 60 spread players from about 11 TPS to 20.
- [22 ms to find 0.73 ms of work](opt-random-ticks.md): random ticks spent most of their time waiting on memory, so we start those reads early and leave the loop as it was.
- [Walking the player list 2,500 times a tick](opt-player-scans.md): a small loop over the player list grew 2.7 times in cost from 60 to 100 players, and a cached copy now gives the same answers.
- [9,900 pairs a tick](opt-locator.md): the locator bar grew faster than the square of the player count, and our fix skips only the pairs that provably send nothing.
- [Where the chunks wait](chunk-send.md): following a chunk from the generation workers to the player, and finding the main thread re-checking unfinished chunks on every tick.
- [Parking the chunks that are not ready](opt-publisher.md): setting unfinished chunks aside, plus a fix to a contended lock, took about 11 ms off each tick at 100 players.
- [Two chunks a second](opt-chunk-load.md): the chunk cost that mattered was a view check behind two caches that wiped themselves when full, and the fix was to raise their caps.
- [Seven terrain workers, and most of them asleep](client-vs-server.md): BDS left most of its chunk generation workers idle, and waking them cut view load times.
- [Choosing which chunk to build next](opt-genscheduler.md): chunk generation spent more time deciding what to build than building it, and the change that helped most was somewhere else.

## The network

- [One network thread is plenty](opt-network-thread.md): one thread sends every packet after login, and a faster compressor cut its peak load from 238 to 62 ms per second without adding threads.
- [Following one movement update through the server](replication.md): a model of who receives each movement update, and a 21-fold gap that made us doubt our own yardstick.
- [Where NetherNet will cost us](nethernet-cost.md): a Windows wait loop whose cost grows with the square of the player count, found before NetherNet becomes the only transport.

## What did not work, or not yet

- [Ten ways to speed up terrain, and why we shipped none](worldgen.md): ten speedups that keep terrain identical, shelved once we found the generation workers asleep.
- [44,700 spawn chunks](opt-view-retry.md): a teleport stall we blamed on the wrong code, caused by a vanilla rule that holds a player's chunk stream in place.
