# Test fakes

Stand-ins for a Minecraft server and the Go load bot. With them, `bbench run-one` runs end to end in seconds, with
no download and no EULA, on Windows and Linux. The integration tests in `tests/integration/` and the adapter contract
test use them. Every file here is standard-library Python except `fake_adapter.py` and `bench.py`, which import the
package.

| File | What it is |
|---|---|
| `fake_server.py` | A scripted server console. It prints one server family's output and accepts bots over TCP. |
| `fake_bots.py` | A drop-in for `loadbot`: the same flags, stdin protocol, exit codes and JSONL events. |
| `fake_adapter.py` | Adapters `fake_bds`, `fake_endstone`, `fake_levilamina`, `fake_pocketmine`, `fake_nukkit`, `fake_powernukkitx` and `fake_dragonfly`. |
| `bench.py` | `FakeBench`: a temporary workspace that runs `bbench` against the fakes. |
| `scenarios/fake-quick.toml` | Four bots, every phase scaled down to seconds. |
| `scripts/*.json` | Behaviour scripts. Each one is also a variant of every fake adapter. |
| `servers/*/server.toml` | Declared parity of the fake adapters: V0 for the BDS family, V1 with absent features for the rest. |
| `test_fakes.py` | Self-tests of all of the above. |

## Console families

The fake server copies the line shapes of each family. The texts around them are synthetic.

| Family | Prefix | Ready line | Tick probe | Positions |
|---|---|---|---|---|
| `bds` | `[2026-10-12 13:58:00:123 INFO] `, continuation lines bare | `Server started.` | `time query gametime` | `querytarget @a` |
| `endstone` | `[13:58:00 INFO]: ` on every line; a launcher starts the server as a child | `Server started.` | `time query gametime` | `querytarget @a` |
| `levilamina` | `13:58:00.123 IFO [Server] `, three-letter levels | `Server started in (…` | `time query gametime` | `querytarget @a` |
| `pocketmine` | `[13:58:00.123] [Server thread/INFO]: ` | `Done (…s)!` | `gametime` (helper) | `positions` (helper) |
| `nukkit` | `13:58:00 [INFO ] `; reads `eng` on stdin first | `Done (…s)!` | `gametime` (helper) | bot-reported |
| `powernukkitx` | `13:58:00 [main] [INFO] ` | `Done (…s)!` | `time query gametime` | bot-reported |
| `dragonfly` | `[13:58:00.123] `, or `2026/10/12 13:58:00 INFO ` for library lines; chat lines bare | `Server started.` | `gametime` (helper) | `positions` (helper) |

The BDS and LeviLamina families print the transport banner at error level after the ready line. Each family's
`Dialect` must treat that banner as benign. The family table in `fake_server.py` holds the patterns that read each
family. `fake_adapter.dialect_for()` compiles them, so a family's output and the patterns that read it cannot drift
apart. `test_fakes.py` also checks that the real `bds`, `endstone` and `levilamina` adapters read the fake output of
their families.

## Behaviour scripts

`bench-script.json` in the server directory changes the fake server's behaviour. A fake adapter copies it there from
`scripts/<variant>.json`. Unknown keys are errors.

| Key | Default | Effect |
|---|---|---|
| `boot_s` | 0.3 | time between the banner and the ready line |
| `tps` | 20 | target tick rate |
| `tick_cost_ms` | 0.3 | CPU burned on the tick thread per tick |
| `tick_mode` | `monotonic` | `stall_without_players` (a world clock) or `reset_at_spawn` |
| `reply_delay_ms` | 0 | extra latency of every command reply |
| `spawn_delay_ms` | 80 | login to player-in-world line |
| `positions` | `ok` | `truncated`, `garbage` or `none` |
| `reject_players` | `[]` | names whose login is refused |
| `drop_every` | 0 | silently drop every Nth command other than the tick probe |
| `chunk_rate`, `corrections_per_s`, `actor_moves_per_s`, `block_updates_per_s`, `locator_per_s` | 120, 0.5, 2, 0.1, 1 | per-bot rates sent to the bots at spawn |
| `banner_version` | none | print this version instead of the arm's |
| `stop_delay_s`, `stop_exit_code`, `ignore_stop` | 0.2, 0, false | shutdown behaviour |
| `rewrite_config` | false | change `server.properties` on shutdown |
| `events` | `[]` | timed actions, below |

An event fires `after_s` seconds after its `anchor`. The anchor is `start`, `ready`, `first_spawn`, `all_spawned` or
`command:<regex>`, which is the first console command matching the regex. The actions are:

- `tps` (`value`)
- `crash` (`exit_code`, `reason`): writes `crash_reports/crash-*.txt`, then exits
- `hang`: stops ticking and stops reading the console
- `probe_gap` (`duration_s`): no replies to the tick probe for that long
- `drop_player` (`player`)
- `decode_error` (`player`): sends the bot a packet it cannot decode
- `kick` (`player`, `reason`)
- `error_line` (`text`)

## Wire protocol

Bots connect to `127.0.0.1:<server-port>` over TCP and exchange one JSON object per line.

- bot to server: `login` (`name`, `i`, `n`, `xuid`, `uuid`, `radius`, `csg`, `session`), `move` (`x`, `y`, `z`),
  `respawn`, `radius` (ignored), `quit`
- server to bot: `spawn` (`x`, `y`, `z`, `dim`, `radius`, `csg`, `rates`), `reject` (`reason`), `tp`, `kick`
  (`reason`), `die`, `respawned`, `dim`, `garbage`

The server clamps the requested radius to its view distance. The bots deliver chunks around their position at the
server's `chunk_rate`, nearest first, with half the radius when client-side generation is on. They emit the events of
`schemas/bot-event.schema.json`.

## Running bbench against the fakes

```python
from fakes.bench import FakeBench

bench = FakeBench.create(tmp_path)
run = bench.run_one("fake_bds", variant="crash-after-join")
assert run.exit_code == 4
```

`FakeBench` sets up the environment for the `bbench` subprocess:

- `PYTHONPATH` gets `tests/` and a throwaway `.dist-info` folder. The folder registers `fakes.fake_adapter` under the
  `bedrock_bench.adapters` entry point group, the way a third-party adapter package registers.
- `BBENCH_ROOT` points at the workspace, which holds copies of `configs/servers/` and `configs/scenarios/` plus the
  generated machine profiles and `configs/matrices/`.
- `BBENCH_CACHE` points into the workspace.
- `BBENCH_BOT_CMD` is a JSON array, the argv prefix that runs `fake_bots.py` in place of the loadbot binary.

The machine profile splits this host's CPUs between server and bots and uses gates loose enough for a busy desktop.
`strict_gates=True` restores the shipped gate values for the contamination tests.

An integration test marks itself with `needs("runner:run_one", ...)`. It stays a strict xfail until every `bbench`
entry point it drives exists, then it runs normally.
