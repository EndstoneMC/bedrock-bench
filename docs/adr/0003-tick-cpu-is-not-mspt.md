# 0003. Tick-thread CPU per tick is not MSPT

- Status: accepted
- Date: 2026-10-12

## Context

MSPT (milliseconds per tick) is the wall-clock time a server spends on one tick. Measuring it directly needs a hook or
the server's own readout: the harness would have to see when each tick starts and ends. bedrock-bench does not hook
into servers, and not every server reports MSPT.

The legacy benchmark published "main thread per tick": the tick thread's CPU time per second divided by TPS. It was
read as MSPT, but it is not:

- CPU time excludes time the tick thread spends blocked: waiting on a lock, on I/O, or on worker threads. A tick that
  waits 20 ms for a worker and computes for 30 ms costs 50 ms of wall time and 30 ms of CPU.
- Dividing by TPS mixes two windows when TPS and CPU are not taken over the same interval. The legacy Windows figures
  divided by the TPS over the whole steady window while the table showed the median of 5 s samples.
- Once the tick thread is saturated, CPU per second stops growing while the work per tick keeps growing.

## Decision

- The metric is called **tick-thread CPU per tick** (`tick_cpu_ms_per_tick_<window>`): `1000 × ΔCPU_tick / ΔG`, where
  ΔG is the number of ticks executed in the window according to the monotonic tick counter. Same window for both terms.
- Its companions are `tick_ms_per_s` (CPU per second), `tick_busy_frac`, per-second utilisation percentiles and
  `tick_saturated_frac` (share of seconds at 95% busy or more).
- Wall-clock time per tick is reported only where it is exact without a hook: `mspt_saturated = 1000 / TPS` when TPS
  is below 19.5, because a saturated server runs ticks back to back.
- A server's own MSPT readout may be added later as `mspt_server_reported`, always shown with its source and never
  ranked across servers.
- Servers without a single tick thread (Go) report these metrics as `not_applicable`; cross-server tables use process
  cores.

## Consequences

- Published figures are honest about what they measure, at the cost of a less familiar name.
- Below saturation, tick-thread CPU per tick is a lower bound on MSPT. A server that blocks often looks better on it
  than it is; `tick_busy_frac` and the TPS figures show when that matters.
- Endstone's legacy "43 ms per tick" maps to `tick_cpu_ms_per_tick_steady` and is checked by claim with a ±15% tolerance
  on the reference machine.
