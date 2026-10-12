# LeviLamina (`levilamina`)

[LeviLamina](https://github.com/LiteLDev/LeviLamina) is a mod loader for Bedrock Dedicated Server on Windows, written
in C++ and licensed LGPL-3.0. A patched copy of Mojang's BDS executable loads PreLoader, which loads LeviLamina and its
mods before the server starts, so gameplay is BDS's own. LeviLamina also routes the server's memory allocations through
mimalloc and drops some malformed packets, so its parity rows in [`server.toml`](server.toml) are fidelity V1. It runs
without mods such as LeviOptimize. Adapter: [`levilamina.py`](../../bedrock_bench/adapters/levilamina.py), built on the
BDS behaviour in
[`bdsfamily.py`](../../bedrock_bench/adapters/bdsfamily.py).

## Setup

```shell
bbench fetch levilamina@26.51.6 --accept-eula
```

- LeviLamina runs on Windows only, and the runner refuses it elsewhere.
- Each release runs on one BDS build (`bds` in its `[artifacts]` table, `1.26.51.1` for 26.51.6), verified and
  installed exactly as for [vanilla BDS](../bds/README.md#setup).
- Two licences cover the downloads: the Minecraft EULA for the BDS zip, and the
  [LeviMC closed-source software EULA](https://github.com/LiteLDev/PreLoader/blob/v1.16.3/EULA.en.md) for PreLoader and
  PeEditor. `--accept-eula` (or `BBENCH_ACCEPT_MINECRAFT_EULA=1`) accepts both. Files already in the cache, or in
  `--artifact-dir DIR` under their release names, need neither.
- Every package is a GitHub release asset pinned with size and sha256, and is placed as the package's own `tooth.json`
  places it (`place` in `[artifacts]`). A package that would overwrite a file already in the install stops the fetch.
- Fetch then runs `PeEditor.exe -mb`, the step `lip install` runs. It writes `bedrock_server_mod.exe` and renames the
  original executable to `bedrock_server.exe.bak`. PeEditor runs once, while the install is built, and never in a run,
  so the install's tree hash covers the executable every run starts. The install is read-only and its tree hash is
  checked before every run.
- The translations package (`levilamina-loc`) is not installed, so LeviLamina's messages are in English, their source
  language and the one the harness parses.
- `--artifact` may point at a local LeviLamina server zip. It replaces only the LeviLamina package. The other
  packages and BDS stay pinned, and the runs are flagged `unpublished_artifact`.

### Adding a release

1. Add its version table to `[artifacts]` in `server.toml`: the BDS build from LeviLamina's `tooth.json`, and one
   `windows-<package>` table per package with the URL, size and sha256 from the GitHub release and the placements from
   that package's `tooth.json`.
2. Start the release once by hand and compare the generated `PreLoaderConfig.json`,
   `plugins/LeviLamina/config/Config.json` with the files here. If their keys or
   config `version` changed, the files here no longer fit both releases, and the adapter needs one set per release.
3. Add a `[features.range."<major>.<minor>"]` to `server.toml` if parity changed, and run
   `bbench smoke configs/smoke.toml levilamina@<version>`.

## How a run starts

```
bedrock_server_mod.exe       measured process, started directly in the run's server copy
  └── CrashLogger.exe        started by LeviLamina, attached to the server
```

- There is no launcher, so `aux_cores` does not apply.
- CrashLogger attaches to the server as a debugger so it can write a report when the server crashes. This is
  LeviLamina's default setup and stays on. CrashLogger's own CPU is not part of `process_cores`. What the attachment
  costs the server is not separated from the server's own CPU.
- Console: `12:00:00.123 IFO [Server] text` on every line, with the logger in brackets. PreLoader logs before LeviLamina
  loads and spells its levels out (`INFO`). Colour codes are stripped before parsing.
- Ready line: `Server started in (5.0s)! For help, type "help" or "?"`. The version banner,
  `Version: 1.26.51.1(ProtocolVersion 2193) with LeviLamina-26.51.6`, must carry the arm's LeviLamina version.
- Tick probe, in-world line, positions and tick thread are those of
  [vanilla BDS](../bds/README.md#how-a-run-starts). LeviLamina's own threads go in the diagnostic groups
  `levilamina_pool` (`ll_default_thread_pool[...]`) and `levilamina_logger` (`logger[...]`), and
  `facts.thread_census_idle` records how many there were.

## Configuration changes

Everything vanilla BDS gets ([`../bds/server.toml`](../bds/server.toml), summarised in
[its README](../bds/README.md#configuration-changes)), plus the JSON configs here. Each is the file the pinned release
writes on its first start (LeviLamina 26.51.6, PreLoader 1.16.3), kept verbatim.

| File here | Written to |
|---|---|
| `PreLoaderConfig.json` | `PreLoaderConfig.json` |
| `LeviLamina.Config.json` | `plugins/LeviLamina/config/Config.json` |

The adapter applies these changes on top and records each as an override:

- `Config.json` `modules.crashLogger.uploadToSentry` = `false` (`harness`): no crash report upload; reports stay in
  `logs/crash`.
- `Config.json` `targeted.enableStatistics` = `false` (`harness`): no usage statistics upload.

The files are written fresh for every run. LeviLamina merges an older config over its new defaults when the config
`version` changes, so the old values win. After the run each file is read back and compared value by value, and a
changed value fails the `config_readback` gate.

Otherwise LeviLamina's generated configuration runs the benchmark as it is.

## Caveats

- LeviLamina 26.51.6 runs on `bds@1.26.51.1`, not the build vanilla BDS runs by default. For a baseline on the same
  build, run both in one session: `bbench run configs/spread-100.toml bds@1.26.51.1 levilamina@26.51.6`. Without that
  baseline the report flags it `different_bds_build`.
- mimalloc is linked into `LeviLamina.dll` and cannot be switched off. It does not show up as a separate module, so
  `facts.allocator` takes it from `allocator` in `[artifacts]`. The allocator is a disclosed confounder
  ([fairness](../../docs/methodology.md#fairness-and-feature-parity)).
- `targeted.vulnerabilityFixes.*` stay on: oversized sub-chunk requests are dropped, item data is checked more strictly
  and connection attempts per IP are limited (`limitIPFrequency`). Unlike the per-IP packet limits of PocketMine-MP,
  Nukkit and PowerNukkitX, this limit is not raised. The bots stay within it, and a dropped bot is flagged `dropped`.
- `targeted.forceEnableCheatCommands` is on by default, so cheats are on in the world. The harness only sends console
  commands, which need none.
- `targeted.checkRunningBDS` refuses a second server from the same directory. Every run has its own copy.
- `targeted.netherNetPatch.*` applies to NetherNet only. The benchmark uses RakNet.
- Log level counts are not compared with other servers.

## Crash files

When the server dies, exits nonzero or never gets ready, the run's `crash/` folder gets the last console lines and the
files written during the run under `logs/crash/` (CrashLogger's reports and dumps) and dumps of
`bedrock_server_mod.exe` in `%LOCALAPPDATA%\CrashDumps`.
