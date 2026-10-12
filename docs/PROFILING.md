# Profiling

bedrock-bench measures; it does not profile. Profilers, thread dumps and tracing change what they observe: a sampling
profiler cost about 6% TPS on a saturated server in our tests, writing its trace made antivirus software scan gigabytes
of data during the run, and Java thread dumps pause the JVM. Results from a profiled run are not results.

When you want to know *why* a server is slow, profile in a separate run.

## Rules

- Never profile a run whose numbers you intend to report or publish.
- Profile the same scenario on the same machine, so the profile explains the load you measured. Use the harness to
  produce that load (`bbench run-one`), and attach the profiler from outside.
- Keep profiler output out of the run directory, and put it on a disk excluded from antivirus scanning if you can.
  Decode traces after the run, not during it.
- Pin the profiler and any trace decoding to the bot CPUs, or run them after the server has stopped, so they do not
  compete with the server.
- If you report findings from a profile, say so, and give the run id of the unprofiled run they explain.

## Producing the load

```shell
uv run bbench run-one --server bds@1.26.52.3 --scenario spread100 --out profiles/
```

The run goes through the normal phases. Watch `run.partial.json` for the marks (`alljoined`, `settled`, `steady0`) to
know when to start and stop the profiler. The run will usually come out `invalid` because the profiler counts as foreign
load; that is expected and is the point: the harness keeps you from mistaking it for a result.

## Tools by platform

| Platform | Native servers | JVM servers (Nukkit, PowerNukkitX) | PHP (PocketMine-MP) | Go (Dragonfly) |
|---|---|---|---|---|
| Windows | Windows Performance Recorder / Analyzer, the Visual Studio profiler | Java Flight Recorder (`jcmd <pid> JFR.start`), async-profiler | PocketMine-MP's built-in timings | `pprof` |
| Linux | `perf record -g`, `perf top` | Java Flight Recorder, async-profiler | PocketMine-MP's built-in timings | `pprof` |

Some practical notes:

- **Sampling rate.** Higher rates cost more. Start at 1 kHz and only go higher for short windows.
- **Start-up delay.** Many collectors take several seconds to start producing samples. Start them before the window
  you care about and discard the first seconds.
- **Which thread.** For tick-rate questions, filter to the tick thread the adapter's tick-thread rule picks (its id is
  in the thread series of the run). Worker threads often look busier while terrain generates, but they are not what
  limits TPS.
- **Separate windows.** Profile the join and the steady state in separate windows; they have different costs.
- **Compare per-tick costs, not percentages.** A fix that raises TPS also changes how much work happens per second
  (more chunks load at 20 TPS than at 11). Compare milliseconds per tick for a given item, not its share of the profile.

## What this project does not cover

Symbolising server frames, reading server internals and anything that depends on non-public material are out of scope
for this repository. Servers are treated as black boxes here; see
[CONTRIBUTING.md](../CONTRIBUTING.md#feature-evidence-rules) for what counts as evidence.
