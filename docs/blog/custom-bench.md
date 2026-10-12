---
title: "Putting four other Bedrock servers through our 100-bot test"
date: 2026-10-12
summary: "We ran the load test behind Endstone 0.11.14 against PocketMine-MP, Nukkit, PowerNukkitX and Dragonfly, and most of the work went into keeping the bots connected."
tags: [benchmarks, performance, pocketmine-mp, nukkit, powernukkitx, dragonfly, endstone-0.11.14]
---

Endstone 0.11.14 holds 20 TPS with 100 players spread across a fresh world. Vanilla BDS manages about 9.4 on the same machine. We wanted to know how that compares with the servers people pick when they want speed.

PocketMine-MP, Nukkit, PowerNukkitX and Dragonfly are all written from scratch without vanilla's full simulation, so we expected them to be fast. Before we could measure any of that, we had to get 100 bots in and keep them there.

The load is the one we use for every Endstone release. A hundred headless clients join in a single wave that takes about 30 seconds. Each one teleports to its own cell of a grid, far enough apart that no two players share chunks, and stays there. View distance is 10 and simulation distance is 4. The server runs on the eight performance cores of an i7-14700F and the bots run on the efficiency cores, so the load generator does not compete with the server for CPU cores.

TPS comes from the server's own game clock, sampled every five seconds. Nukkit and PowerNukkitX ship with `auto-tick-rate` on, which lets a slow world tick less often while its clock still jumps ahead by the skipped ticks. A TPS reading based on that clock looks healthy when the server is struggling, so we turned it off for every run.

## Getting through the door

PocketMine-MP 5.44.3 would not let the bots in at all. The project announced end of support in July 2026, and its last release speaks protocol 1001 (Minecraft 1.26.30), older than our bot's. We rebuilt the bot from the last gophertunnel release for 1001. PocketMine still rejected every login with "Required property ... is missing in JSON data" until we back-ported two login changes from a later version.

Nukkit and PowerNukkitX let the first bot in and kicked it as soon as the second arrived, giving a login from another location as the reason. Both derive a player's UUID from the Xbox user ID, and our offline bots all sent the same empty one, so every new bot looked like the previous player logging in twice. A numeric fake ID per bot stopped the kicks. Nukkit then kicked everyone for flying, because our bots hover where they are teleported. On BDS the server corrects each bot's position and the bot falls to the ground. We set `allow-flight` on Nukkit.

The first full PocketMine run looked like a chunk-send stall. Every bot timed out at the same moment, rejoins failed, and the server sat at 20 TPS with nobody on it. The answer was one log line: "Blocked 127.0.0.1 for 300 seconds".

PocketMine's network layer bans any IP that sends 200 datagrams within a 10 ms window. All 100 bots share 127.0.0.1, so a join wave looks like a flood from a single attacker. Three of the four servers had a per-IP limit like this, and we raised it on PocketMine, Nukkit and PowerNukkitX. Dragonfly's network library only blocks malformed packets.

With the bans gone, PocketMine crashed 23 seconds into the join. Its default 1 GB memory limit for the main thread is too small for 100 players with separate views. We recorded the crash, set the limit to 0, which turns it off, and ran again.

## Fresh-world results

| Server | Steady TPS | Lowest TPS while views load | CPU cores | Memory (GiB) |
|---|---:|---:|---:|---:|
| Endstone 0.11.14 | 20.0 | 16.5 | 1.04 | 4.7 |
| Dragonfly (`1793369`) | 20.0 | 14.6 | 0.56 | 1.9 |
| PocketMine-MP 5.44.3 | 4.6 | 1.5 | 1.07 | 6.0 |
| PowerNukkitX 3.0.5 | 1.3 | 0.7 | 3.67 | 15.8 |
| Nukkit (`b3caad6`) | shut down | | | |

Memory is the server's private memory at the end of the steady window. Endstone's row comes from its 0.11.14 release run. In the same session as the other servers, its lowest TPS while views loaded was 18.6.

Dragonfly also holds 20 TPS, on 0.56 cores where Endstone uses 1.04. It runs a flat world with no mobs and no server-side movement checks.

Nukkit logged all 100 bots in, but spawns took up to two minutes and 63 bots gave up at their 60-second deadline. Five minutes in, its first autosave began writing about 17,600 chunks on the main thread. After 60 seconds of that, its watchdog shut the server down. We list it as a shutdown because that is what a server owner would have seen.

PowerNukkitX kept its main thread busy for about 900 ms of every second and used 3.7 cores. Its memory sat at the JVM default heap, a quarter of the machine's RAM. Its views never filled. After the join, our harness runs a settle phase that lasts at least five minutes and ends once players are receiving fewer than 0.1 new chunks per second each. PowerNukkitX was already below that rate at the five-minute mark, so the settle phase ended there with the median view at 16 chunks. A server that stops sending chunks looks the same as one that has finished, so the next version of the harness will also check how full each view is.

## A fair second chance

A fresh world is hard on these servers, so we pre-generated worlds and ran again.

Nukkit pre-generated through a plugin: 53,802 chunks in 460 seconds. On that world it joined all 100 bots and held 20 TPS steady. While views were still loading it ran at about 5.7 TPS.

PowerNukkitX's generate call did nothing. Its generator drops requests for chunks with no player nearby. We sent 10 bots on a tour of the grid cells instead and let its real pipeline do the work. On that world it reached 4.3 TPS. A Java stack dump showed why. In eight of ten samples, the main thread was reading the chunks it ticks from disk and inflating them with zlib while the tick waited. Garbage collection took about 3%, so a bigger heap would not have helped.

We hoped to share one world between servers, and every pairing we tried failed. PowerNukkitX regenerated chunks from a Nukkit world. Dragonfly could not read Nukkit's world file. A PowerNukkitX world made Dragonfly fall back to flat chunks and then panic a few minutes in.

## What the packets showed

The bots record every packet they receive, and that record changed how we read the table.

On BDS each bot gets about 2.9 movement corrections per second, which is why it falls and lands. On all four other servers the bots hovered at y=301.6 for the whole run. None of them sent a single correction, and none of them implements the locator bar.

None of them supports client-side chunk generation either. In every run where views filled, the median bot received between 453 and 480 unique chunks. On Endstone a bot receives about 103, because the client builds the rest of the terrain itself. [Seven terrain workers, and most of them asleep](client-vs-server.md) explains how client-side generation came to be on by default.

A TPS figure only means something next to the work behind it. In the harness we are building now, every result will carry movement corrections and chunks per player, and servers that simulate different things will not be ranked against each other.
