# LeviLamina configuration notes

The JSON files here are written into every LeviLamina run. Each one is the file the pinned release generates on its
first start (LeviLamina 26.51.6, PreLoader 1.16.3, LeviOptimize 0.17.0), kept verbatim, except
`LeviOptimize.all.json`, which is the `levioptimize-all` variant's config. The adapter applies the changes listed
below on top and records each one as an override in `run.json`.

| File here | Written to | Arms |
|---|---|---|
| `PreLoaderConfig.json` | `PreLoaderConfig.json` | every arm |
| `LeviLamina.Config.json` | `plugins/LeviLamina/config/Config.json` | every arm |
| `LeviOptimize.default.json` | `plugins/LeviOptimize/config/config.json` | `+levioptimize` |
| `LeviOptimize.all.json` | `plugins/LeviOptimize/config/config.json` | `+levioptimize-all` |

The files are written fresh for every run because both projects change them on their own. LeviLamina merges an
older config over its new defaults when the config `version` changes, so the old values win. LeviOptimize saves
its in-memory config over the file when the server stops. A server directory reused between runs could therefore
carry values nobody chose.

## Changes from the generated files

| File | Key | Generated | Benchmark | Category | Why |
|---|---|---|---|---|---|
| `Config.json` | `modules.crashLogger.uploadToSentry` | `true` | `false` | harness | no crash report upload during measurement; reports stay in `logs/crash` |
| `Config.json` | `targeted.enableStatistics` | `true` | `false` | harness | no usage statistics upload during measurement |
| LeviOptimize `config.json` | `features.optMovingBlock` | `false` | `true` | nonvanilla | `levioptimize-all` only |
| LeviOptimize `config.json` | `features.optSeenPercent` | `false` | `true` | nonvanilla | `levioptimize-all` only |
| LeviOptimize `config.json` | `features.optPushEntity.enable` | `false` | `true` | nonvanilla | `levioptimize-all` only |

`levioptimize-all` switches on every LeviOptimize option that is a switch. `optPushEntity.maxPushTimes` is a limit
rather than a switch and stays at `-1` (no limit), as in the legacy runs.

After the run, every file is read back and compared value by value with what was written; a difference fails the
`config_readback` gate. A file the server saved again with the same values passes.

## Left at the generated defaults

| Key | Default | Effect on a benchmark |
|---|---|---|
| `language` | `system` | none: the translations package is not installed, so messages are in English |
| `targeted.forceEnableCheatCommands` | `true` | cheats are on in the world; the harness uses console commands, which need none |
| `targeted.vulnerabilityFixes.*` | all `true` | drops malformed or oversized packets and limits connection attempts per IP; the bots stay within the limits, and `dropped == 0` is checked |
| `targeted.netherNetPatch.*` | on, two public STUN servers | NetherNet only; the benchmark uses RakNet |
| `targeted.checkRunningBDS` | `true` | refuses a second server from the same directory; every run has its own copy |
| `modules.crashLogger.enabled` | `true`, external | CrashLogger runs as a separate process attached to the server and writes reports to `logs/crash` |
| `logRotate.*` | daily, compressed | log files under `logs/`; the harness reads the console |
| PreLoader `colorLog` | `true` | colour codes are stripped from the console before parsing |

## Not switchable

LeviLamina routes the server's memory allocations through mimalloc, which is linked into `LeviLamina.dll`. There is
no setting to turn it off. The allocator is declared in `server.toml` and recorded in
`facts.allocator`, because it does not show up as a separate module.

## Adding a release

1. Add its version table to `[artifacts]` in `server.toml`: the BDS build from LeviLamina's `tooth.json`, and one
   `windows-<package>` table per package with the URL, size and sha256 from the GitHub release, and the placements
   from that package's `tooth.json`.
2. Start the release once by hand and compare the generated `PreLoaderConfig.json`,
   `plugins/LeviLamina/config/Config.json` and LeviOptimize `config/config.json` with the files here. If their keys
   or config `version` changed, the files here no longer fit both releases, and the adapter needs one set per
   release.
3. Add a `[features.range."<major>.<minor>"]` to `server.toml` if parity changed, and run
   `bbench smoke levilamina@<version>`.
