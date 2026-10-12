---
title: "Our bots had never moved"
date: 2026-10-12
summary: "Every load test we ran before 10 October used bots that never moved."
tags: [performance, profiling, benchmarks, load-testing, windows, bds]
---

Where does the main server thread of Bedrock Dedicated Server 1.26.51 spend its time when many players are spread far apart across a world? We wanted that answer from vanilla, with nothing of ours loaded.

The setup had limits. We had a Windows workstation without admin rights, and other work shared the same machine. Vanilla has no plugin API, so every number had to come from outside the server.

## The bots that stood still

Our load bot is a small Go program built on gophertunnel. Each bot joins, spawns and walks. At least, that was the plan.

Then we typed `querytarget @a` into the console, which lists every player's position. We ran it twice, a few seconds apart. Nobody had moved. The server had also sent zero movement corrections. Our bots fake walking on flat ground, and on real terrain that normally draws about one correction per second. Zero meant they were not moving at all.

A real client tells the server when its loading screen has closed. Until then, the server holds the player in place and ignores its movement. Our bot never sent that message. From the server's point of view, every bot sat on a loading screen forever. None of them fell, and none of them got new chunks as they "walked".

We now send that message right after spawn, and the bots walk. It also meant every bot run before 10 October had measured frozen players, so their capacity numbers were too optimistic. With walking bots on default settings, vanilla 1.26.51 held about 11 TPS at 60 spread players.

Smaller changes followed. Bots scattered into ungenerated land now get protective effects and a teleport to y=300, so they fall safely once their terrain arrives. The steady correction from fake flat walking is a cost a real client would not cause, so we now report the correction rate next to any movement numbers.

## A join that would not end

Our first real run put 60 bots 640 blocks apart, each asking for a view radius of 16 chunks. They all joined within 18 seconds. The main thread stayed saturated for more than ten minutes. TPS sat between 9 and 14 while the bots together received about 80 new chunks per second.

Our instinct was to wait for the chunk rate to drop and call the join finished. That signal lies. When the main thread is full, chunks arrive slowly because the thread is busy, and loading is far from done. We switched to watching how many unique chunks each bot held. When that count stopped growing and TPS stopped climbing, the join was over. At 60 bots that took 17 minutes, and TPS settled around 11 without ever getting back to 20. At 120 bots the join took more than half an hour.

The profile of that join pointed at the autosave picker, which took 57% of the main thread. That one has [its own post](chunk-storage.md).

## A profiler without admin rights

Most system profilers on Windows need an elevated prompt. The sampling collector that ships with Visual Studio does not. It attached to the running server with normal user rights and sampled 4,000 times per second.

It captured the whole system. Decoded to text, a trace grew by about 17 MB for every second captured, so a single trace came to 2 to 5 GB. Usable stacks showed up 5 to 10 seconds after we pressed start, so we dropped the first 12 seconds of every trace.

The first time we decoded a trace, the machine got busy in a way that had nothing to do with the server. Windows Defender had noticed gigabytes of fresh text on disk and was scanning all of it on 2 to 3 cores. We stopped writing the text. The decoder now streams straight into our parser, so Defender has nothing to read, and a 75-second trace decodes in about 50 seconds.

A few percent of the samples landed in memory that no module claimed. That puzzle became [another post](codegen.md).

## Counting ticks from the console

Vanilla has no TPS counter, and we did not want to modify the server we were measuring. The console had what we needed. Every 5 seconds the harness typed `time query gametime`. Game time advances by one each tick, so TPS over a window is the change in game time divided by the wall time.

The profile gave us a second clock. At 4,000 samples per second, a gap of more than 0.75 ms between main thread samples means the thread was idle. Those gaps mark where one tick ends and the wait for the next begins.

## Measuring the measurement

The profiler is not free. At saturation it cost about 6% of TPS. So every phase got two windows, one unprofiled for the headline numbers and one profiled for the breakdown.

Our own harness took CPU too, and at one point a separate Endstone A/B test ran its own server and bots next to ours. We started logging machine load for every window so a busy machine shows up next to the number it affected.

## Before and after

When the autosave fix went in, we profiled again and nearly misread the result. At 20 TPS the walking bots covered more ground than at about 11, so the server ended up holding 100,000 unique chunks instead of 71,000. Every percentage in the profile moved because the total moved. We now compare costs in milliseconds per tick, never in percent of the main thread.

Even with the fix, the 60 bots took 803 seconds to load their views. Chunk delivery climbed to 150 to 180 per second between 400 and 600 seconds in, and that pulled TPS down to about 18 for a minute. We now budget about 15 minutes of settling per 60 spread players.
