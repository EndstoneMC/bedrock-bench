# 0005. Client-side generation off in every ranking scenario

- Status: accepted
- Date: 2026-10-12

## Context

[ADR 0004](0004-legacy-faithful-headline.md) kept the legacy workload for the headline scenario `spread100`, including
client-side chunk generation (CSG) on. CSG skews chunk work about 4.4-fold: with it, BDS sends about 100 chunks per
player at view distance 10; without it, the full disc of about 450
([PITFALLS.md](../PITFALLS.md#server-and-config-parity)). PocketMine-MP, Nukkit, PowerNukkitX and Dragonfly cannot do
CSG at all.

Under 0004, Endstone ran its headline with CSG on, while the cross-server table ran every server with it off. Comparing
Endstone with CSG on against the custom servers gives it about a quarter of their chunk work. Even against itself,
Endstone's numbers would mean different work depending on which table a reader looked at.

## Decision

- Every ranking scenario runs CSG off in the server config and the bot, Endstone included. `spread100` is the headline
  for every server, and every server builds the full disc, 317 columns at view distance 10. 0004's separate
  equal-chunk scenario is gone.
- The legacy CSG-on workload survives only as `spread100-csg`, to reproduce the figures in Endstone's BENCHMARK.md on
  BDS-hosted servers. `report --publish` refuses a cross-server table from it.
- The rest of 0004's workload parameters stay: grid, join stagger, distances, seed, difficulty, 5 s probe, settle rule,
  load window, interventions and steady window.
- `spread100` changes in place, although 0004 expected a new headline to get a new name; no results had been published
  from it.

## Consequences

- Endstone's new numbers will not match its BENCHMARK.md, which ran with CSG on.
- The claims on the published figures moved to the CSG-on matrix: `claims/endstone-0.11.14-csg.toml`, checked with
  `bbench report runs/endstone-0.11.14-csg --claims endstone-0.11.14-csg`.
- The CSG-off headline has no published figure to compare against. Its first publication sets a new baseline.
- Endstone's own matrix and the cross-server matrix run the same scenario.
