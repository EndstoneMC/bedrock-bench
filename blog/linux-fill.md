---
title: "Filling in the Linux column"
date: 2026-10-12
summary: "Measuring vanilla BDS and two Endstone releases on Linux for the 0.11.14 benchmark page took a day of harness fixes, and our check against vanilla turned up a locator bar bug in vanilla's own Linux build."
tags: [benchmarks, linux, docker, methodology, locator-bar]
---

The day before Endstone 0.11.14 shipped, our benchmark page had a full Windows table and an empty Linux one. Filling it meant three kinds of run in Docker, each against vanilla BDS 1.26.52, Endstone 0.11.13 and Endstone 0.11.14.

The first is the capacity run. 100 players join at once, each sent to its own part of a fresh world, and we measure TPS, main thread time per tick, CPU, memory and how long each player waits for their view to load. The second is chaos: 15 minutes of randomized admin actions against those 100 players, including joins and kicks, teleports, deaths, dimension changes and random tick speed changes anywhere from 0 to 1000. The third is an instrumented Endstone build that runs vanilla's own code next to ours on every call during a chaos run and compares the results.

Most of the day's fixes were to our own tooling, and we cover those first. The most interesting result came from the third run.

## Harness paper cuts

Vanilla failed on its first launch with "permission denied". We had unpacked the BDS zip with Python's `zipfile` module, which does not keep the executable bit. A `chmod +x` in the container's start command fixed it. Vanilla's console also prints timestamps in a different format from Endstone's and has no `status` command, so our log parser and the script that finds the server process both had to learn it.

The rest were small. A build setting for the instrumented build was silently overridden by our packaging tool, so the build succeeded without its instrumentation. We now check the built library for proof that the switch is on. We tested a stale copy of our in-server test plugin. A long folder name pushed one file past Windows' 260-character path limit. Our job queue was a bash loop, and pausing it with `kill -STOP` held only until the current job exited, when the loop started the next one anyway. The queue now waits on a file between jobs.

## The commands that never ran

We report chaos TPS only for the time spent at normal tick speeds of 0, 1 and 3. Our first version split the run by the moment the harness sent each tick speed command.

That split was wrong. In one Linux run, 2 of the 31 tick speed changes never happened. Endstone's console drops a command that arrives while the server's input queue is full. That is a bug in Endstone, and 0.11.14 still has it. On Windows every change ran, but some arrived up to two and a half minutes late. Either way, parts of the run were filed under the wrong tick speed.

We now split by the line the server prints when a game rule actually changes. With that, Linux chaos TPS at normal tick speed came out at 14.8 for vanilla, 10.2 for 0.11.13 and 19.3 for a 0.11.14 development build.

## A number that was too low

Our first Linux chaos run on that build read 17.2 TPS at normal tick speed. The rerun read 19.3. The build had not changed. The first run had overlapped a Windows chaos run on the same machine, and each of those runs drives 100 bots. Before we trust a number now, we check it against the times of any other run on the machine.

## The check that failed on Linux

The locator bar is the row of dots at the top of the screen that shows where other players are. To keep it right, the server looks at every ordered pair of players, a viewer and the player being viewed, on every tick. Our 0.11.14 change skips the pairs where it can prove nothing would be sent and hands every other pair to vanilla's own code. [9,900 pairs a tick](opt-locator.md) covers that change in full.

On Windows the instrumented build had found nothing. On Linux, player lookup and chunk publishing came back clean as well. The locator bar did not. In one 15-minute chaos run, 24 million of 101 million locator bar checks disagreed with vanilla.

We assumed our change was broken on Linux and went looking for the bug in our code.

The clue was a second count the check keeps. The locator bar holds one record for each pair. With 23 players online that is 23 × 22 = 506 pairs. Our table had 506 records. Vanilla's had 504, and every record it did have matched ours. Vanilla was losing pairs.

The cause was in vanilla. Its Linux build files each pair's record under a weak hash of the two players, and two pairs with the same hash share one record. At 100 players, between 46% and 66% of pairs share a hash with at least one other pair. On Windows the collisions do not happen.

Pairs that share a record keep overwriting each other's last sent position, so each one looks changed on every tick, and vanilla on Linux sends their positions again every tick. At 100 players that adds about 23,000 updates per second. In other cases a player's first position is never sent, and their dot is missing from another player's locator bar.

Endstone keeps exactly one record per pair, so on Linux it behaves like vanilla on Windows. Part of the 0.11.14 locator bar gain on Linux comes from skipping that collision cost. Every disagreement the check reported traces back to those collisions. Across all its checks, the instrumented build found 0 differences in 833 million on Linux and 745 million on Windows, with the Linux locator bar as the one exception.

## The column

This is what went onto the page for Linux, with 100 spread-out players:

|                              | Vanilla 1.26.52 | Endstone 0.11.13 | Endstone 0.11.14 |
|------------------------------|----------------:|-----------------:|-----------------:|
| Steady TPS                   |            9.46 |             9.45 |             20.0 |
| Main thread per tick         |        105.7 ms |         105.9 ms |          46.5 ms |
| View load time, median       |           194 s |            192 s |            130 s |
| Chaos TPS, normal tick speed |            14.8 |             10.2 |             19.3 |
| Memory (RSS)                 |         3.96 GB |          4.06 GB |          4.54 GB |

The chaos row for 0.11.14 was measured on a development build from before the packet compression and terrain generation changes. The extra memory comes from mimalloc, which 0.11.14 uses on Linux.

One weak spot does not show in the table. When all 100 players join at the same moment, TPS drops for about 30 seconds while the server takes in the newly generated terrain. The lowest 15-second average was 8.0 on Linux and 17.5 on Windows. Steady TPS is not affected.

## What we took away

The most useful line in the day's logs was a count. Vanilla's table held 504 records for 506 pairs, and none of the 504 differed from ours. That line moved the search from our code to vanilla's.
