# Workloads

A workload is a scenario file in `scenarios/`. One scenario drives every arm of a matrix (rule R1), and its hash is
stored in every run, so results from different scenario versions are never mixed. This page describes the shared
parameters, how the bots are placed and behave, every scenario, and the chaos actions.

## Common parameters

All `spread100*` scenarios share these settings.

| Parameter | Value | Note |
|---|---|---|
| Bots | 100, one join every 300 ms | about 30 s from first dial to all joined; staggered, not simultaneous |
| Login | offline, self-signed, deterministic UUIDv5 per bot name, RakNet over 127.0.0.1 | no encryption |
| Grid | 11 columns, 640 blocks apart, origin 1280 | 640 blocks = 40 chunks, more than twice the view distance, so no two bots share a chunk |
| Placement | effects, then `tp <bot> x 300 z`, once the server says the bot is in the world | never `spreadplayers` |
| Movement | circle walk; the client never changes its own height | servers that validate movement land the bots |
| Chunks | client cache on, every cached chunk reported as a miss; full-column sub-chunk requests | worst case, applied equally to every server |
| Server | view distance 10, simulation distance 4, seed 20261010, level `bench`, difficulty easy, survival, max 200 players, autosave every 6000 ticks, player data saved, one level loaded, telemetry off where configurable | per-server keys in [FAIRNESS.md](FAIRNESS.md) |
| World | fresh: the world directory is absent before launch and generated during the run | `world.fresh` and the generator type are recorded |
| Probe | every 5 s through the console priority lane | keeps the 5 s and 15 s dip figures comparable with the legacy numbers |

### Grid

Bot `i` goes to cell

```
cell(i) = (1280 + (i % 11) * 640, 1280 + (i // 11) * 640)
```

so 100 bots fill 9 full rows of 11 and one bot in the tenth row. The scenario loader refuses a spacing that is not
larger than two view distances in blocks. Spreading the bots like this is the worst case for a server: every player
needs its own terrain generated, loaded, ticked and sent. Clustered bots share chunks and flatter the server.

### Placement

When the bot reports `spawned` and the server prints its "player in world" line (the adapter's `player_in_world`
pattern), the harness sends:

1. effects: resistance 255, fire resistance, water breathing and saturation 255, infinite (or the server's maximum),
   particles hidden, so bots do not die from falls, lava, water or hunger;
2. `tp <bot> <x> 300 <z>` to the bot's cell.

Teleporting high above ungenerated terrain lets the server generate the area while the bot falls. `spreadplayers`
into ungenerated chunks put bots at unpredictable heights and is not used. A bot whose teleport is not confirmed within
20 s gets the commands again (up to 3 times, settle only).

### Bot behaviour

The same Go bot runs against every server ([bots/README.md](../bots/README.md)). Per bot, it:

- logs in offline with a deterministic identity (UUIDv5 of the scenario's identity seed and the bot name);
- tells the server its loading screen has ended after spawning and after every dimension change; without this, a
  server may never process the bot's movement;
- requests chunk radius 16, which servers clamp to their view distance;
- walks a circle at 0.2158 blocks per tick, turning 0.01 rad per tick (one lap about every 628 ticks), sending its
  input 20 times per second; start directions are spread by the golden angle (bot index × 137.508°);
- answers latency probes, respawns after death and acknowledges dimension changes;
- counts unique chunks per (dimension, x, z), and which of them are within its current view, so chunks streamed for an
  old position do not count;
- reports progress every 5 s, aggregate counters every 10 s and the received packet mix every 60 s, as JSON lines.

A decode error in the bot makes the run invalid; it means the bot did not see everything the server sent.

## Scenarios

| Scenario | Purpose | Differences |
|---|---|---|
| `smoke` | Check an adapter end to end | 4 bots, view distance 4, settle 30-60 s, steady 20 s. Every dialect command is sent once and must not error; decode errors must be 0; the probe must advance at idle; movement corrections must appear where the feature file declares movement validation. |
| `spread100` | Endstone vs vanilla headline: identical work among BDS-hosted servers | Client-side generation on where the server supports it; the bots announce support for it. |
| `spread100-equal` | Cross-server headline | Client-side generation off in the server config and the bot. Every server builds and sends the full disc, 317 columns at view distance 10. |
| `spread100-pregen` | Delivery without generation | Each server loads a world template built by its own pipeline (`bbench prepare-world`), validated at 99% per-cell coverage. Client-side generation off. |
| `chaos-portable` | Robustness across servers, never ranked | After spread, 15 minutes of actions every adapter supports, at normal tick speed. |
| `chaos` | Full stress for BDS-family servers | Adds the actions only BDS-family servers support, including random tick speed cycling. |

### What each scenario exercises

Every scenario lists the features whose parity rows the report must show (`exercises`) and the features that must
match for efficiency metrics to be comparable (`requires`). For `spread100`:

```
terrain.vanilla_generator, chunks.client_side_generation, chunks.subchunk_requests,
player.server_authoritative_movement, player.physics, player.locator_bar, world.autosave
```

`spread100-equal` and `spread100-pregen` do not exercise client-side generation. The scenario files are authoritative;
`run.json` records the fully resolved scenario (`scenario.resolved`).

### World templates

`bbench prepare-world <server@version> --scenario spread100-pregen` builds a template with the server's own pipeline:

- by default a bot tour: a few bots visit every cell at view distance 12 with client-side generation off;
- for Nukkit, a helper plugin that generates the cells directly.

The template is accepted only when every cell reaches 99% coverage of its view disc. Its sha256 is recorded in each run
that uses it. Templates stay in the local cache. They are never committed, published or shared between servers, because
world formats are not interchangeable between server families.

## Chaos

Chaos scenarios run after the steady window. They are robustness checks: they look for crashes, stalls, lost players
and memory growth under rapid admin actions. They are never used to rank servers.

### Rules

- The plan is generated before the run from `(seed, scenario, n)` and saved as `chaos-plan.jsonl` with its hash. The
  same seed gives the same plan.
- Each action fires at its catalogue interval multiplied by U(0.6, 1.4). The first firing is at U(3, min(interval, 60))
  seconds.
- Targets are bot indices. If the target bot is offline at that moment, the action is recorded as `skip_offline`.
- Nothing is skipped because of backlog. Commands go through the paced console writer and each records when it was
  scheduled, sent and applied. If the writer falls more than 60 s behind, no new actions start and the run is flagged
  `chaos_overrun`, which is a result.
- An action the server has no equivalent for is counted as `unsupported` and never sent.
- Bot radius jitter (a new radius between 4 and 16 every 20 s on average) starts at chaos start, not before.
- Tick-speed regimes are taken from the server's own "applied" log lines, never from when the command was queued.
  5 s TPS samples that straddle a regime change are labelled `mixed` and excluded. `normal` = random tick speed 0, 1
  or 3; `high` = 100 or 1000. Cross-server chaos figures use normal tick speed only.

### Action catalogue

Intervals are in seconds (`scenarios/chaos-actions.toml`).

| Action | Interval | What it does | In `chaos-portable` |
|---|--:|---|:-:|
| `churn` | 20 | a group of bots disconnects and reconnects | yes |
| `stack` | 25 | several bots are teleported onto one spot | yes |
| `gamemode` | 15 | a few bots switch game mode and back | yes |
| `kill` | 30 | a bot is killed and respawns | yes |
| `kick` | 40 | a bot is kicked and rejoins | yes |
| `far_tp` | 15 | a bot is teleported far away into unloaded terrain | yes |
| `view_disconnect` | 30 | a bot disconnects while its view is still loading | yes |
| `weather` | 45 | the weather changes | yes |
| `list` | 10 | the player list is queried | no |
| `summon` | 30 | mobs are summoned near a bot | no |
| `cleanup` | 90 | summoned entities are removed | no |
| `dimension` | 20 | a bot moves to the Nether or the End and back | no |
| `blocks` | 20 | blocks are filled or set near a bot | no |
| `tickingarea` | 40 | a ticking area is added or removed | no |
| `waypoints` | 60 | the `playerwaypoints` game rule switches between `off` and `everyone` | no |
| `tickspeed` | 30 | `randomtickspeed` steps through 0, 1, 3, 100, 1000, 1 | no |

`chaos` uses the full catalogue. The command each action sends is defined per adapter (its chaos dialect); the adapter
contract test checks that every action is either rendered or explicitly unsupported.

## Planned workloads

- **Capacity sweep:** the largest bot count that holds 20 TPS (steady TPS at least 19.8 and lowest 15 s at least 19.0),
  for n from 25 to 200.
- **Entity-dense and redstone-dense:** a platform at y = 200 in every cell with a fixed mob mix or observer clocks.
- **Parity probes:** short active tests of gravity, impossible moves, the locator bar, mob movement, redstone, fluids
  and terrain match, to observe features that passive counters cannot.
