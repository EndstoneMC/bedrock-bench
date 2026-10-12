# 0004. Keep the legacy workload for the headline scenario

- Status: superseded by [0005](0005-csg-off-headline.md)
- Date: 2026-10-12

## Context

Endstone 0.11.14 published figures from the legacy harness. The new harness fixes many of that harness's measurement
problems ([PITFALLS.md](../PITFALLS.md)), and three design proposals suggested changing the workload too: a 1 s tick
probe with interpolated percentiles, a fixed 330 s load window, difficulty normal instead of easy. Each change would be
defensible on its own. Together they would make it impossible to tell whether a difference from the published numbers
comes from the servers, from the workload or from a bug in either harness.

## Decision

The headline scenario `spread100` keeps the legacy workload parameters:

- 100 bots on an 11-column grid 640 blocks apart, joining 300 ms apart, effects and `tp` to y = 300;
- view distance 10, simulation distance 4, client-side generation on, seed 20261010, difficulty easy (the BDS default);
- a tick probe every 5 s, so the lowest 5 s and 15 s figures keep their meaning;
- the settle rule: at least 300 s, at most 900 s, ending at 0.1 new in-view chunks per bot per second over 60 s;
- the load window `[bots_start, settled]`;
- bounded nudges and re-teleports during settle only;
- a 90 s steady window starting 15 s after settle.

Measurement fixes that do not change the workload are applied: monotonic timestamps, chunk first-seen times, in-view
filtering, the dimension key, process CPU counters, repeats, interference gates. Where a fix changes a figure, the
expected direction is listed in [REPRODUCING-endstone-0.11.14.md](../REPRODUCING-endstone-0.11.14.md). The legacy 12 s
pause before the join is available as `pre_bots_s` but off by default.

## Consequences

- The published direction and magnitude of the Endstone comparison can be checked, with claims and tolerances in
  `claims/endstone-0.11.14.toml`.
- Some legacy choices that are not ideal stay in the headline: client-side generation makes chunk work unequal across
  server families, and fresh-world generation dominates the load window. Separate scenarios cover those
  (`spread100-equal`, `spread100-pregen`), and cross-server tables use `spread100-equal`.
- A future headline with a different workload will be a new scenario with a new name, not an edit of `spread100`.
