# Endstone (`endstone`)

[Endstone](https://github.com/EndstoneMC/endstone) is a plugin API for Bedrock Dedicated Server. It hosts Mojang's
BDS binary of a fixed build and runs it through its own launcher (`python -m endstone`). Gameplay is BDS's own.
From 0.11.14 Endstone also replaces some server paths with faster ones that are meant to behave identically, so its
parity rows (`features/endstone.toml`) are fidelity V0 for 0.11.13 and V1 for 0.11.14.

Adapter: `src/bedrock_bench/adapters/endstone.py`, built on the shared BDS behaviour in `adapters/bdsfamily.py`.
Configuration notes for the pinned releases: `servers/endstone/README.md`.

## Versions

| Version | Hosts BDS | Default Python | Linux allocator | Known issues |
|---|---|---|---|---|
| 0.11.13 | 1.26.52.3 | 3.13 | glibc | Windows: the hidden DevTools window keeps a core busy; both: the scheduler's idle threads use CPU |
| 0.11.14 | 1.26.52.3 | 3.13 | mimalloc (preloaded by the launcher) | none |

Both are compared with vanilla BDS 1.26.52.3, the build they host, so no `different_bds_build` flag applies. Known
issues come from Endstone's changelog and appear as `known_issue` footnotes; their CPU cost is part of the measured
numbers and is never subtracted. Wheels are pinned per platform and CPython version (3.11 to 3.14) in
`artifacts/endstone.toml`; an arm picks one with `python = "3.13"`.

## Setup

```shell
bbench fetch endstone@0.11.14 --accept-eula
```

Fetching needs [uv](https://docs.astral.sh/uv/). It does three things:

1. Verifies and installs BDS 1.26.52.3 exactly as for vanilla BDS (`docs/servers/bds.md`), under the same EULA rule.
2. Downloads the Endstone wheel for this platform and Python from PyPI and checks it against its pinned sha256.
3. Builds a virtual environment in the cache (`venv/endstone-<version>-cp<python>-<hash>`): the wheel's
   dependencies from `servers/endstone/requirements/<version>.txt` with `--require-hashes`, then the verified wheel
   without dependency resolution, then `uv pip check`. The environment is made read-only and its tree hash is checked
   before every run.

An arm can point `artifact` at a local wheel instead. Its results are marked `unpublished_artifact`; when no lock
exists for its version, its dependencies are resolved from the index and the install records `deps_lock_sha256: null`.

On Windows, add the loopback exemption for the Minecraft app once, from an elevated prompt, so the launcher never asks
for elevation during a run (see `servers/endstone/README.md` for the command).

## How a run starts

```
<venv>/python -m endstone -y --no-interactive -s .      (working directory: the run's server copy)
  └── bedrock_server(.exe)                              measured process
```

- The launcher compares `version.txt` in the server directory with the BDS build it supports. The adapter writes the
  marker (`26.52` for BDS 1.26.52.3), so the launcher uses the verified copy instead of downloading BDS again. The
  sha256 of `bedrock_server` is taken before and after the run; a difference would show that the launcher replaced it.
- The launcher's environment gets `PYTHONSAFEPATH=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUNBUFFERED=1` and
  `PYTHONDONTWRITEBYTECODE=1`, and loses `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV` and, on Linux, `LD_PRELOAD`.
- The server process is the launcher's `bedrock_server` child, found by its executable path. Endstone renames the
  server's main thread to `Main`, which on Linux also renames the process, so the process name is never used.
- The launcher process is measured separately and reported as `aux_cores`. On Windows a child process does not
  inherit an above-normal priority class, so the runner sets the server's priority on the child itself.

## Overrides

Everything vanilla BDS gets (`docs/servers/bds.md`, `servers/bds/properties.toml`), plus:

| File | Key | Shipped | Benchmark | Category | Reason |
|---|---|---|---|---|---|
| `version.txt` | contents | absent | `26.52` | harness | the launcher's installed-BDS marker; without it BDS is downloaded again |
| `plugins/bstats/config.toml` | `enabled` | `true` | `false` | harness | no usage statistics upload during measurement |
| environment | `PYTHONSAFEPATH` and the other variables above | inherited | set or removed | harness | the environment runs exactly as installed |
| environment | `LD_PRELOAD` (variant `glibc-alloc`) | unset | `libc.so.6` | parity | glibc's allocator, as vanilla BDS uses |

`endstone.toml` is created by the launcher with Endstone's defaults and left alone. Its only setting with a visible
effect here is `network.ipv6 = false`: Endstone binds IPv4 only, while vanilla BDS also binds `server-portv6`. The
bots use IPv4.

The launcher also appends a rate limit for NetworkStackLatency (packet 115: a bucket of 20, drained at 5 per second)
to `packetlimitconfig.json` at every start. Vanilla BDS has no limit for that packet. The bot answers the server's
own latency requests and sends none of its own unless `probe_bots` is raised; keep it at 0 in comparisons with
vanilla. The file as found after shutdown is kept in the run's `config/`.

Endstone's crash reporter uploads a report when the server crashes. The pinned releases have no switch for it, which
`facts.crash_upload` records.

## Variant `glibc-alloc`

Linux only, 0.11.14 and later: preloads `libc.so.6`, so the launcher keeps glibc's allocator instead of preloading the
mimalloc it bundles. It isolates the allocator's share of a Linux difference between Endstone and vanilla. 0.11.13
already runs on glibc's allocator, so the variant is refused there. `facts.allocator` records the allocator each run
actually mapped.

## Console

| | |
|---|---|
| Line format | `[12:00:00 INFO]: text`, or `[12:00:00 INFO]: [Logger] text`; every line of multi-line output carries the prefix; the launcher's own lines use the same format |
| Levels | `TRACE`, `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (`CRITICAL` counts as fatal) |
| Version banner | `This server is running Endstone version: 0.11.14 (Minecraft: 26.52)`, which must equal the arm's version |
| Ready, tick probe, in world, positions, stop | as vanilla BDS |

Log level counts are not compared with other servers: Endstone and BDS do not log the same events at the same levels.

## Parity

Endstone runs BDS's own gameplay, so every feature is declared `vanilla`. For 0.11.14 the rows that its changelog
names as optimised (terrain generation, client-side generation, locator bar, random block ticks) carry a note, and
their counters (`csg_effective`, `corrections_per_bot_s`, `airborne_frac_steady`, `locator_pkts_per_bot_s`) are printed next
to the declaration. Endstone's own equivalence tests are not used as evidence by this harness.

## Crashes

Collected on a crash or a nonzero exit: `crash_reports/` (Endstone's crash report, `crash-<date>-server.txt`),
`.sentry-native/`, and `bedrock_server` dumps in `%LOCALAPPDATA%\CrashDumps` on Windows.
