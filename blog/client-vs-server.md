---
title: "Seven terrain workers, and most of them asleep"
date: 2026-10-12
summary: "We set out to learn why BDS runs a third as many terrain workers as the Minecraft game on the same PC, and found that it left most of its own workers idle."
tags: [performance, chunk-generation, threading, benchmarks, endstone-0.11.14]
---

When we started the performance work that became Endstone 0.11.14, one number on our benchmark machine looked wrong. The machine runs Windows 11 on an Intel Core i7-14700F, with 8 performance cores and 12 efficiency cores, 28 hardware threads in total. Vanilla BDS ran 7 worker threads for chunk generation on it. The Minecraft game hosts single-player and LAN worlds with the same engine, and on the same PC it ran 23.

Our benchmark leans hard on exactly those workers. It puts 100 players in a fresh world, each in their own corner of the map, and they all join at the same moment. We call the first stretch after that the join storm, because every player needs terrain at once. On vanilla the median player waited more than three minutes for their view to fill in. More workers looked like the obvious way to shorten that wait.

## A detour through client-side generation

Before counting workers, we compared how the game and the server were set up. The first difference that turned up had nothing to do with workers. Bedrock can let the client build part of the world itself from the seed. With client-side generation on, the server sends each player the chunks closest to them and the client generates the outer ring. BDS turns this off for flat, void and legacy worlds. Endstone's command line was also writing `client-side-chunk-generation-enabled=false` into every new server.

That changes what a benchmark measures. On a flat world, or with the setting off, the server sends every chunk over the network. In our comparison with other servers, Endstone sends about 100 chunks to each player, while servers without client-side generation send about 450. Since 0.11.14, new Endstone servers keep the vanilla default of `true`, and every number in our benchmark runs with it on.

## Counting cores

Next we worked out how the server decides how many workers to start. It takes the smaller of two numbers, the `max-threads` setting in `server.properties` and a count of the machine's cores, and starts one fewer than that. The shipped default for `max-threads` is 8.

On Windows the server counts only cores that run two threads. On CPUs where every core runs two threads, that is every core. On a hybrid Intel chip only the performance cores count, because the efficiency cores run one thread each. On our machine that gave 8, and so 7 workers. On Linux the server counts every processor instead, but the default `max-threads=8` still holds it to 7 workers there until an admin raises the setting.

We did not track down exactly how the game arrives at 23. A number that high has to include the efficiency cores, which the server's count leaves out.

The pool size sets more than the number of workers. The code that picks the next chunk to generate scales its queue to the pool, so a smaller pool also means a shorter queue of work. That code has its own post, [Choosing which chunk to build next](opt-genscheduler.md).

The first thing a server admin would reach for here is a higher `max-threads`. We tried `max-threads=16`, and the server still started 7 workers. The core count is a hard ceiling. On a Windows PC with more than 8 two-thread cores, such as a 12- or 16-core Ryzen, raising the setting does add workers. On a hybrid Intel chip like ours, no setting gives terrain more workers.

## The workers that slept

To see what more workers would buy, we ran a test-only copy of the server with that ceiling lifted. It started 15 generation workers. Only 4 of them ever did any work.

We went back to the stock server and watched its 7 workers while 100 players joined. Two or three of them generated terrain. The other four or five sat idle for the whole join storm.

The pool wakes an extra worker only once a sizeable backlog has built up for every worker that is already busy. A generation task finishes in a couple of milliseconds, so the backlog almost never got deep enough.

## The fix

Endstone 0.11.14 sets that backlog threshold to zero, so any queued generation task wakes an idle worker (commit `cd97eea16`). The server keeps its own pool size and now uses all of it.

This is a timing-only change. Every chunk's terrain comes from the world seed and the chunk's own position, so which worker builds a chunk, and when, does not change what it contains. Chunks arrive sooner and are otherwise identical.

We measured it with 100 spread players on a 0.11.14 development build. That build already carried our earlier fixes, so its median view load before this change was 132 s rather than vanilla's 192 s.

- With a small non-zero threshold, all 7 workers woke. The median view load fell from 132 s to 86 s, and the 90th percentile from 248 s to 205 s. TPS dipped to 18.2 during the join storm.
- With the threshold at zero, the version we shipped, all 7 workers woke as well. TPS averaged 19.9 over the join storm and never fell below 19.2. We stopped that run once the joins were done, so it has no view load numbers of its own.
- Steady TPS and memory did not change in any run that went past the joins.

The test server with 15 workers and 4 awake reached a median of 83 s, about the same as 7 awake. Once 4 or more workers are busy, something else sets the pace on this machine. The main thread and the code that sends finished chunks to players are our main suspects, and we have not pinned it down yet. So we shipped the wake fix and left the pool size alone.

In hands-on play with client-side generation off, we flew one player fast through a fresh world and watched all seven workers share each burst of new terrain. A trip through a Nether portal loaded in under three seconds.

## Where that leaves us

Together with the rest of the 0.11.14 changes, the released build brings the median view load on Windows to 81 s, against 192 s for vanilla. When all 100 players join at once, its lowest 15-second TPS average is 17.5.

What holds view loading back once 4 workers are busy is the next thing we want to measure.
