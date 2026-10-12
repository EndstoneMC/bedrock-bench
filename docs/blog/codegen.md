---
title: "We went looking for a compiler problem"
date: 2026-10-12
summary: "Before changing any game logic, we checked how Bedrock Dedicated Server was built and then profiled the smaller costs it pays as it runs."
tags: [performance, profiling, compression, memory-allocator, windows, linux, endstone-0.11.14]
---

Our performance work started with a stubborn number. Sixty players spread across a fresh world held vanilla Bedrock Dedicated Server 1.26.51 at about 11 TPS on Windows. The main thread spent around 85 ms on each tick, and a tick has a budget of 50.

Endstone does not compile BDS. We load into Mojang's release binary and change its behaviour from the inside. So before touching any game logic, we wanted to know what the compiler had already decided for us.

Our first suspect was the build itself, such as a release shipped with optimizations turned down. We checked how the Windows build was made. It is an ordinary optimized release build with link-time optimization, and it does not use profile-guided optimization. The code was compiled sensibly, so the slowness had to come from what the code does. We reached for a profiler.

## A fixed tax

We profiled a 60-player run on Windows. About 2 percent of the main thread's samples went to Control Flow Guard, the Windows security feature that checks the target of an indirect call before the jump.

It is a security feature, so we treated it as a fixed tax and moved on.

## The allocator

Memory allocation was the next slice: about 2 percent of the main thread in steady play and 8 percent while players joined and terrain flooded in.

On Linux the server gets its memory from the system C library, so an allocator loaded ahead of it takes over the whole process. We tried mimalloc in an A/B run on Linux with the same seed and the same spread bots. That run used an Endstone development build that already had the autosave fix described below, so it started from a much faster server than the 11 TPS above.

At 50 players the main thread went from 47.0 to 36.6 ms per tick, 22 percent less. At 60 players the default allocator fell to 17.1 TPS while mimalloc held 20.0. Memory use went up by 13 to 15 percent. We ran each configuration once, so treat it as one data point.

The same swap on Windows is harder. The obvious shortcut is to replace the allocator for the server's own code only. The Microsoft C and C++ runtime libraries the server loads also allocate memory, and objects cross between the server and those libraries. Memory freed by a different allocator than the one that created it corrupts the heap.

The safe options are to redirect the runtime itself or to guard against pointers that came from the other allocator. For now, Endstone 0.11.14 uses mimalloc on Linux only.

## A compressor for every batch

One stack from a network thread on Windows sat inside the Windows heap's routine for handing memory back to the operating system. Directly above it was zlib's setup.

The server builds a new zlib compressor for every batch of packets it sends, uses it once and throws it away. Each one needs a few hundred kilobytes of working memory. On Windows the heap returns large freed blocks to the operating system, so the next batch faults the same memory straight back in. Compression happens per connection, so a broadcast pays this once per player.

We had already gone after compression on our development branch, and [One network thread is plenty](opt-network-thread.md) covers what it did for the thread that sends packets. In August we replaced the compression step there with libdeflate, and each connection now keeps its compressor for as long as it lives. After this profile we back-ported that change to 0.11.14.

To measure it we recorded 39.7 MB of real gameplay traffic, 3,465 batches in all. Vanilla's zlib compressed it at 25.5 MiB/s. libdeflate did 117.9 MiB/s, 4.6 times faster, with output 0.40 percent larger. A higher libdeflate setting would still be 1.6 times faster than vanilla and about 1 percent smaller.

A synthetic benchmark had put libdeflate at only 1.85 times faster. Real Minecraft batches are highly compressible, which is why the synthetic numbers fell so far short. Had we trusted them, we would have undersold the change by more than half.

libdeflate writes standard deflate streams, so a client decodes them exactly as before. When the change first landed we checked both directions against real zlib.

## What it added up to

None of these findings was the big one. At 60 players [the autosave picker](chunk-storage.md) alone took 45 percent of the main thread. In an A/B run on vanilla BDS with only that fix patched in, the server went from roughly 10 to 14 TPS to a steady 20.

In a new profile with the autosave fix in place, the smaller costs took up more of the picture. Control Flow Guard's share rose from 2.0 to 3.7 percent while its cost per tick barely moved. Allocation grew to about 4 percent in steady play and 15 percent during joins. Percentages drift whenever the total changes, so from then on we compared costs in milliseconds per tick.

The same profile showed where to look next. Random ticks took about 16.4 ms per tick, a third of the main thread. Redstone checks took about 7.0 ms, or 14 percent.

Endstone 0.11.14 ships the compressor change on both platforms and mimalloc on Linux, alongside the autosave fix and several other performance changes.

Our 100-player benchmark runs BDS 1.26.52 with view distance 10, so it does not compare directly with the 60-player runs above. With the whole release in place, Endstone holds 20 TPS there on both Windows and Linux. Vanilla manages about 9.4.
