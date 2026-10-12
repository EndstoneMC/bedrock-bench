# Reading results

A bedrock-bench report is built to stop the most common misreadings of server benchmarks. This page explains how to
read one, in the order the report presents it.

## Six rules

### 1. 20 TPS is a ceiling, not a score

A Bedrock server targets 20 ticks per second and never runs faster. Two servers that both hold 20 TPS can be far apart:
one may use 40% of its tick thread, the other 95%. Add a few players and the second one drops. To compare servers
that both hold 20 TPS, read the headroom metrics:

- `tick_cpu_ms_per_tick` is the tick thread's CPU time per executed tick. At 20 TPS a tick has a 50 ms budget, so
  43 ms per tick means little headroom is left even though TPS is perfect.
- `tick_busy_frac` and `tick_saturated_frac` show how much of the time the tick thread was busy and for how many
  seconds it was fully busy.

Below 20 TPS, the tick thread is saturated and TPS itself is the measure. `mspt_saturated` (1000 / TPS) is then the
real time per tick. See [ADR 0003](adr/0003-tick-cpu-is-not-mspt.md) for why tick-thread CPU is not called MSPT.

Servers written in Go have no single tick thread, so their tick-thread metrics are `not_applicable`; compare process
cores instead.

### 2. Read parity before cost

Every table starts with the parity rows: for each feature the scenario exercises, whether the server implements it the
way vanilla BDS does (`vanilla`), partly (`partial`), with a deliberate shortcut (`altered`), or not at all (`absent`).
A server that skips player physics, mob AI or terrain generation does less work per tick and is cheaper for that reason,
not because it is better engineered.

Servers that are `vanilla` on every exercised feature are listed under "Full vanilla fidelity"; the rest under
"Reduced fidelity", each with a "does less than baseline" list. The two sections are never ranked against each other,
and there is no composite score ([ADR 0002](adr/0002-no-composite-score.md)).

Declared parity comes from `features/<server>.toml` with an evidence link per cell ([FEATURES.md](FEATURES.md)). Where
a counter can observe the feature, the report prints the observed value next to the declaration and flags
`parity_mismatch` when they disagree.

### 3. Same workload is not same work

Every server receives the same bots doing the same things, but servers respond differently:

- **Chunks.** BDS with client-side chunk generation sends about 100 chunks per player at view distance 10; servers
  without it send the full disc of about 450. `spread100` compares only BDS-hosted servers for this reason, and
  `spread100-equal` turns client-side generation off everywhere for cross-server tables.
- **Movement.** Servers that validate movement send corrections, servers with physics make the bots fall to the
  ground, servers without either leave the bots hovering at the teleport height (visible in the y distribution).
- **Other traffic.** Actor moves, block updates and locator-bar packets per player per second.

The work table sits next to the cost table so you can see what each server did for its CPU time. Efficiency metrics
such as `cpu_ms_per_player_tick` are only comparable between arms with the same parity on the scenario's `requires`
list.

### 4. Ranges, not points

Each cell shows the median of at least 3 runs with its minimum and maximum. "A better than B" is printed only when
every run of A beats every run of B; otherwise the report prints `~`, which means "not separated on this evidence", not
"equal". Ratios come with a range. Run-to-run noise on tick-thread CPU is around ±15 ms per second on a desktop, so a
small difference between two medians usually means nothing.

### 5. Failures are results

A server that crashes, is shut down by its own watchdog, or cannot take every player in time is `server_failed`. The
report shows it as `k/n ok` in the status column and never folds it into a median. A footnote says what happened.
Overrides in the `rescue` category (a config change without which the default setup fails the workload) always carry a
footnote that states the default-config outcome, so you know what an out-of-the-box install would do.

`invalid` is different: the harness or the machine failed (contention, a lost probe, a bot that could not decode a
packet). Invalid runs are rerun once and are not results about the server.

### 6. Pick the lens for your use case

- **Survival server with many players spread out:** steady TPS, tick-thread CPU per tick, lowest TPS while views load,
  view-load time, under full vanilla fidelity.
- **Lobby or minigame server:** players are clustered and mostly need a stable tick and low cost per player. Reduced
  fidelity may be acceptable if the missing features do not matter for your game. Read the "does less" list.
- **Capacity planning:** look at headroom, not TPS, and remember the numbers come from one machine.

## Anatomy of a report

1. **Conditions block.** Harness version and commit, machine (CPU model, core classes, OS, power plan), placement
   (`physical` or `vcpu`), scenario name and hash, bot build and protocol, repeats and order. Different conditions mean
   different tables.
2. **Parity rows** for the exercised features, declared and observed.
3. **Cost table.** TPS (steady, lowest 5 s and 15 s while views load), tick-thread CPU per tick, process cores, memory.
4. **Work table.** Chunks per player, corrections, actor moves, block updates, locator packets, packet mix, effective
   client-side generation, world type.
5. **Status column.** `n_ok/n_total` per arm, with failure footnotes.
6. **Footnotes** generated from flags and overrides: `rescue` and `nonvanilla` overrides, `known_issue`,
   `different_bds_build`, `bot_protocol_differs`, `terrain_mismatch`, `unpublished_artifact`, `vcpu` placement.

## Things that look comparable but are not

- **Memory across operating systems or runtimes.** Windows private bytes and Linux RSS measure different things. A JVM
  server's memory is mostly its fixed heap.
- **Log error counts across servers.** Each server logs differently. Counts are shown per server for diagnosis only.
- **Join time.** Measured on the bot's clock from dial to spawn. It does not include chunk delivery and is not a
  headline metric.
- **Settle duration.** It has a 300 s floor and depends on the settle rule, so it is not a loading-speed metric. Use
  the view-load metrics instead.
- **Chaos TPS.** Shown only at normal tick speed with delivered and unsupported action counts. Servers that cannot run
  some actions did less, so chaos is never ranked.
- **Results from different scenario hashes, machines or analysis versions.** The report refuses to put them in one
  table.

## Worked example

The legacy Endstone 0.11.14 comparison (Windows, 100 spread players, one run each) read like this:

| | Vanilla BDS 1.26.52 | Endstone 0.11.13 | Endstone 0.11.14 |
|---|--:|--:|--:|
| Steady TPS | 9.40 | 11.28 | 20.0 |
| Tick-thread CPU per tick | 105 ms | 89 ms | 43 ms |

How to read it with the rules above:

- Vanilla and 0.11.13 are saturated: TPS is below 20 and the tick thread is busy almost all the time, so their
  per-tick figures are close to the real time per tick, 1.8 to 2.1 times the 50 ms budget. 0.11.14 holds 20 TPS with
  43 ms of tick-thread CPU per tick, so it is not saturated but has only about 14% headroom left at this load.
- All three run the same BDS build with client-side generation on and are vanilla on every exercised feature, so the
  comparison is within one fidelity section.
- Each figure is a single run, so none of the differences meet the separation rule. The new harness repeats each arm at
  least 3 times. [REPRODUCING-endstone-0.11.14.md](REPRODUCING-endstone-0.11.14.md) explains how to check these
  figures, and [PROVENANCE.md](../published/legacy-0.11.14/PROVENANCE.md) where each came from.
