# Endstone (`endstone`)

[Endstone](https://github.com/EndstoneMC/endstone) is a plugin API for Bedrock Dedicated Server, written in C++ with
Python bindings and licensed Apache-2.0. It runs Mojang's BDS binary of a fixed build through its own Python launcher
(`python -m endstone`), so gameplay is BDS's own. 0.11.14 also replaces some server paths with faster ones that are
meant to behave identically. Adapter:
[`endstone.py`](../../bedrock_bench/adapters/endstone.py), built on the BDS behaviour in
[`bdsfamily.py`](../../bedrock_bench/adapters/bdsfamily.py).

## Setup

```shell
bbench fetch endstone@0.11.14 --accept-eula
```

Fetch needs [uv](https://docs.astral.sh/uv/). It does three things:

1. Verifies and installs the BDS build the release hosts (`bds` in its `[artifacts]` table) exactly as for
   [vanilla BDS](../bds/README.md#setup), under the same EULA rule.
2. Downloads the Endstone wheel for this platform and CPython version from PyPI and checks it against its pinned sha256.
   Wheels are pinned for CPython 3.11 to 3.14. The benchmark uses `python` from `[benchmark]`; otherwise the release's
   `python` applies.
3. Builds a virtual environment in the cache: the dependencies from `requirements-<version>.txt` with
   `--require-hashes`, then the verified wheel without dependency resolution, then `uv pip check`. The environment is
   read-only and its tree hash is checked before every run.

`--artifact` can point at a local wheel. Its runs are flagged `unpublished_artifact`. When no lock exists for its
version, its dependencies are resolved from the index.

On Windows the launcher checks that the Minecraft app may connect to loopback, and asks for elevation to add the
exemption if not. Add it once from an elevated prompt, so no prompt appears during a run:

```shell
CheckNetIsolation LoopbackExempt -a -p=S-1-15-2-1958404141-86561845-1752920682-3514627264-368642714-62675701-733520436
```

### Adding a release

1. Add its version table and wheel pins to `[artifacts]` in `server.toml`, with URL, sha256 and size from
   `https://pypi.org/pypi/endstone/<version>/json`.
2. Generate its lock, with the release's lowest supported Python as `--python-version`:

   ```shell
   echo "endstone==<version>" | uv pip compile --universal --generate-hashes --python-version 3.11 \
     --no-emit-package endstone --no-header - -o servers/endstone/requirements-<version>.txt
   ```

3. Run `bbench smoke configs/smoke.toml endstone@<version>`.

## How a run starts

```
<venv>/python -m endstone -y --no-interactive -s .     working directory: the run's server copy
  └── bedrock_server(.exe)                             measured process
```

- The adapter writes `version.txt` with the release's `version_marker` (`26.52` for `bds@1.26.52.3`). The launcher
  compares it with the BDS build it supports and uses the verified copy instead of downloading BDS again. The server
  binary's sha256 is taken before and after the run, and a change fails the `harness_ok` gate.
- The measured process is the launcher's `bedrock_server` child, found by its executable path. Endstone renames the
  server's main thread to `Main`, which on Linux also renames the process, so the name is never used.
- The runner applies the server CPU set and priority to the child again, because on Windows a child does not inherit an
  above-normal priority class.
- The launcher's process tree, without the server, is measured separately and reported as `aux_cores`.
- Console: `[12:00:00 INFO]: text` or `[12:00:00 INFO]: [Logger] text` on every line, the launcher's own lines
  included. The version banner, `This server is running Endstone version: 0.11.14 (Minecraft: 26.52)`, must equal the
  arm's version.
- Ready line, tick probe, in-world line, positions and tick thread are those of
  [vanilla BDS](../bds/README.md#how-a-run-starts). An extra diagnostic thread group, `crash_reporter`, holds the
  threads named `sentry*`.

## Configuration changes

[`server.properties`](server.properties) is the same file as vanilla BDS's, summarised in
[its README](../bds/README.md#configuration-changes). Also:

- `plugins/bstats/config.toml`, rendered from [`bstats.toml`](bstats.toml): `enabled = false`, so no usage statistics
  are uploaded during measurement.
- `version.txt`: the release's `version_marker`. Without it the launcher downloads BDS over the verified copy.
- Launcher environment: `PYTHONSAFEPATH=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUNBUFFERED=1` and
  `PYTHONDONTWRITEBYTECODE=1` are set. `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV` and, on Linux, `LD_PRELOAD` are
  removed, so the environment runs exactly as installed.

`endstone.toml` is created by the launcher with Endstone's defaults and left alone. The adapter writes
`server.properties` itself and the launcher never downloads BDS, so the launcher's defaults for new servers
(`server-name`, `view-distance` and, before 0.11.14, `client-side-chunk-generation-enabled=false`) never apply.

## Caveats

- On Linux, 0.11.14 preloads the mimalloc it bundles and 0.11.13 uses glibc's allocator. `facts.allocator` is
  `mimalloc` when the server maps it and `default` otherwise. The allocator is a disclosed confounder
  ([fairness](../../docs/methodology.md#server-configuration)).
- At every start the launcher appends a rate limit for NetworkStackLatency (packet 115: a bucket of 20, drained at 5
  per second) to `packetlimitconfig.json`. Vanilla BDS has no limit for that packet. The bot only answers the server's
  latency requests unless `bots.probe_bots` is raised, so keep it at 0 when comparing with vanilla BDS.
- Endstone binds IPv4 only (`network.ipv6 = false` in its default `endstone.toml`), while vanilla BDS also binds
  `server-portv6`. The bots use IPv4.
- Endstone uploads a crash report when the server crashes. The pinned releases have no switch for it, which
  `facts.crash_upload` records.
- Each release's known issues (`known_issues` in `[artifacts]`, from Endstone's changelog) are recorded in
  `facts.known_issues`. For 0.11.13 these are the hidden DevTools window that keeps a core busy on Windows and the
  scheduler's idle threads that use CPU on both platforms. Their cost stays in the measured numbers.
- Endstone and BDS do not log the same events at the same levels, so log level counts are not compared with other
  servers.

## Crash files

When the server dies, exits nonzero or never gets ready, the run's `crash/` folder gets the last console lines and the
files written during the run under `crash_reports/` (Endstone's `crash-*.txt`), `.sentry-native/` and, on Windows,
dumps of `bedrock_server` in `%LOCALAPPDATA%\CrashDumps`.
