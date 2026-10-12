# Feature parity files

Two servers can run the same workload and still do different amounts of work: one generates vanilla terrain and
validates every movement, the other sends a flat world and trusts the client. A result is only meaningful next to
what the server actually implements, so every server declares its parity against vanilla BDS here, one file per
server (`features/<server>.toml`). Reports print these rows next to every cost table, and `docs/FEATURES.md` is
generated from them.

## File format

```toml
schema = 1
server = "pocketmine"

[range."5.44"]                     # version prefix: matches 5.44 and 5.44.x; the longest matching prefix wins
fidelity = "V1"
"player.physics" = { class = "absent", observed_by = "airborne_frac_steady", note = "bots keep hovering at y=300" }
"terrain.structures" = { class = "partial", evidence = "https://example.org/docs/generator" }
```

A version that matches no range has no parity row, which a published table treats as `unknown`. Give every pinned
version a range of its own when its behaviour differs from its neighbours.

## Fidelity

The optimisation layer of a build as a whole:

| Fidelity | Meaning |
|---|---|
| `V0` | Vanilla code paths. The server runs Mojang's implementation unchanged, or is Mojang's server. |
| `V1` | Equivalent behaviour. Some paths are replaced with faster ones that claim the same observable results. |
| `V2` | Changed gameplay. The build deliberately skips or alters work to go faster (for example skipping block ticks). |

## Classes

| Class | Meaning |
|---|---|
| `vanilla` | Implemented, with the same observable behaviour as vanilla BDS. |
| `partial` | Implemented, with parts missing or behaving differently. |
| `altered` | Changed on purpose to save work, for example an optimisation option that skips moving blocks. |
| `absent` | Not implemented. |
| `unknown` | Not assessed. Never allowed in a published table for a key the scenario `exercises`. |

## Evidence

Every class other than `unknown` needs evidence: a link (`evidence`) to source code, documentation or a changelog,
an observed counter (`observed_by`, a metric id from docs/METRICS.md), or both. When a counter observes a feature,
reports print the declared class and the observed value side by side and flag `parity_mismatch` when they disagree.
Claims a project makes about itself, such as its own test suite, count as evidence for that project's declaration
only; the harness never presents them as its own measurement.

## Keys

Adding a key means adding it here and to `FEATURE_KEYS` in `src/bedrock_bench/model.py` in the same change.

| Key | Vanilla behaviour | Observed by |
|---|---|---|
| `terrain.vanilla_generator` | Overworld terrain from Mojang's generator, identical to BDS for the same seed. | |
| `terrain.any_generator` | Terrain is generated at all (not only a flat or void world). | |
| `terrain.structures` | Villages, temples and other structures are placed during generation. | |
| `chunks.client_side_generation` | Clients that support it generate terrain from the seed; the server sends less. | `csg_effective` |
| `chunks.subchunk_requests` | Chunk columns are delivered through sub-chunk requests. | |
| `entity.mob_spawning` | Mobs spawn naturally around players. | |
| `entity.mob_ai` | Mobs path-find, target and act. | |
| `entity.collision_push` | Entities push each other apart on collision. | |
| `entity.item_entities` | Dropped items exist as entities, merge and despawn. | |
| `player.server_authoritative_movement` | The server checks player movement and corrects the client when it disagrees. | `corrections_per_bot_s` |
| `player.physics` | Players fall, collide with blocks and land; a client cannot hover by claiming to. | `airborne_frac_steady` |
| `player.hunger_damage` | Hunger, fall damage and other player damage apply. | |
| `player.locator_bar` | Each player receives the positions of other players for the locator bar. | `locator_pkts_per_bot_s` |
| `block.redstone` | Redstone components update and propagate signals. | |
| `block.random_ticks` | Random block ticks at the `randomtickspeed` game rule (crops, leaves, fire, ...). | |
| `block.fluids` | Water and lava flow. | |
| `block.falling` | Sand, gravel and other gravity blocks fall. | |
| `world.lighting` | Sky and block light are computed. | |
| `world.dimensions` | The Nether and the End exist and can be travelled to. | |
| `world.autosave` | The world is saved to disk periodically while running. | |
| `commands.vanilla_set` | The vanilla command set (`tp`, `effect`, `gamerule`, `querytarget`, ...) is available. | |
| `net.encryption` | Online-mode logins and packet encryption. Benchmarks run offline, so this is never exercised. | |
