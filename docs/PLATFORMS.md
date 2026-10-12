# Platforms

bedrock-bench runs on Windows and Linux, natively or with Linux servers in Docker. This page covers what each platform
needs and what it can measure.

## Support matrix

| | Windows 10/11 x64 | Linux x86_64 native | Docker (Linux engine) | Docker Desktop, WSL2 |
|---|---|---|---|---|
| runner, analyze, report | yes | yes | yes | yes (placement `vcpu`, report warning) |
| per-thread sampling | Win32 API via ctypes | `/proc` | sampler sidecar sharing the server's PID namespace | sampler sidecar |
| pinning | CPU sets; `auto` uses the efficiency class | sysfs `cpu_core`/`cpu_atom` or an explicit list | `--cpuset-cpus` | virtual CPUs only |
| bds, endstone | yes | yes | yes | yes |
| levilamina | yes | no | no | no |
| pocketmine, nukkit, powernukkitx, dragonfly | yes | yes | yes | yes |

macOS can run `bbench analyze`, `report` and `validate` on existing run directories, but cannot run servers.

Published Linux numbers should come from bare-metal Linux. Docker Desktop and WSL2 runs are fine for development and
are labelled `vcpu`.

## Before a measured run

`bbench doctor` checks most of this and prints what it finds; `--json` gives the same report for scripts.

- **Quiet machine.** Close browsers, chat clients, game launchers and any Minecraft client. Stop other benchmarks and
  builds. The idle gate refuses to start under foreign load, and runs contaminated during a measured window are
  invalid.
- **Power.** On Windows use the High performance or Ultimate Performance plan and keep the machine on mains power. On
  Linux use the `performance` governor. Turbo and frequency scaling stay enabled but must be the same for every arm.
- **Thermals.** Long runs on a laptop or in a small case may throttle. If results drift from one repeat to the next,
  check the CPU's temperature and clock speed during a run.
- **CPU budget.** The bots need at least 4 logical CPUs outside the server's set. On a 100-bot run they use one to three
  cores; a run whose bot CPUs are more than 85% busy is invalid.

## Hybrid CPUs

On CPUs with performance and efficiency cores (for example Intel 12th to 14th generation), `server_cpus = "auto"` puts
the server on the performance cores and `bot_cpus = "auto"` puts the bots on the efficiency cores.

- Windows reports the core class through the CPU-set efficiency class.
- Linux reports it through `/sys/devices/cpu_core/cpus` and `/sys/devices/cpu_atom/cpus`.
- On CPUs without core classes, set explicit CPU lists in the machine profile. `doctor` prints the topology it
  detected either way.

Never put a measured server on efficiency cores, and never let it share cores with the bots. The run records the core
classes it found.

## Windows

- **Suspended launch.** Servers start with `CREATE_SUSPENDED`, get their CPU set and priority, and then resume, so JVM
  and BDS thread pools are sized for the CPUs they actually get.
- **Thread CPU** comes from thread times (100 ns units). Cycle counts are an optional secondary column converted with a
  frequency calibrated at startup. Thread names come from thread descriptions, where the server sets them.
- **Memory** is private bytes (commit charge) and working set, from process memory counters.
- **Stopping the bots.** The bot stops on `quit` from stdin. If it does not exit within 10 s it gets `CTRL_BREAK` (it
  runs in its own process group), which Go maps to an interrupt so the bot still prints its summary.
- **Microsoft Defender.** Real-time scanning of the run directory and the cache costs CPU during runs, especially while
  logs are written. Add exclusions for the cache root (`%LOCALAPPDATA%\bedrock-bench`) and your `runs/` directory if
  your policy allows. Foreign-load sampling shows Defender as a foreign process when it interferes.
- **Path length.** Windows limits most paths to 260 characters (`MAX_PATH`). The cache uses short hashed directory names
  so server trees stay under the limit. Keep the cache root short; set `BBENCH_CACHE` to something like `C:\bb` if your
  profile path is long. Enabling long paths in Windows does not help every program a server runs.
- **Console.** Commands are written with LF line endings and UTF-8 encoding.
- **Priority.** `above_normal` is available for matrices that need it (`endstone-0.11.14` and `endstone-0.11.14-csg`).
  The default is `normal`, the same for every arm.

## Linux

- **Affinity** is set in the child before `exec`, so the server's first thread already has it.
- **Thread CPU** comes from `/proc/<pid>/task/<tid>/schedstat` (nanoseconds). If it is unavailable the harness falls
  back to `stat` fields 14 and 15 divided by `sysconf(SC_CLK_TCK)`. The `stat` line is parsed after the last `)`,
  because thread names such as `REST Thread(0)` contain spaces and parentheses.
- **Memory** is RSS (`VmRSS`) and private memory (`RssAnon` plus `VmSwap`) from `/proc`.
- **BDS on Linux** needs the executable bit (the harness extracts archives with their file modes) and
  `LD_LIBRARY_PATH=.` (set by the adapter). It has four threads named `MC_SERVER`; the tick thread is the one with the
  lowest thread id.
- **Endstone on Linux** preloads mimalloc by default. The allocator is recorded in `run.json`; the `glibc-alloc` variant
  runs it with glibc's allocator instead.
- **Stopping the bots:** `quit` on stdin, then `SIGINT` after 10 s.
- **cgroups and cpusets.** Some servers size thread pools from the online CPU count, not from the cpuset. The thread
  census at idle records the pool sizes so this is visible.

## Docker

The Docker runtime runs Linux servers in containers on a Linux engine. Set `runtime.kind = "docker"` in the machine
profile.

- **Bots in the server's network namespace.** The bots run in a container started with
  `--network container:<server>` and connect to 127.0.0.1. Publishing the server's UDP port with `-p` instead goes
  through Docker Desktop's userspace UDP proxy, which delivered about a tenth of the native chunk traffic in our tests
  and made the server look healthy because it was underloaded. `doctor` compares a Docker smoke's chunk total with a
  native one and warns when they differ by more than 20%.
- **Sampler sidecar.** Per-thread sampling runs in a small container started with `--pid container:<server>`, which
  reads `/proc` for the server's threads and stamps each sample after reading it.
- **Pinning** uses `--cpuset-cpus` for the server, bot and sampler containers.
- **Images** are pinned by digest (`artifacts/images.toml`). BDS is mounted into the container from the verified cache,
  never baked into an image. Endstone is installed from PyPI with hash checking.
- **Console input** is written with LF line endings. Text-mode pipes on a Windows host would send CRLF, which some
  servers read as part of the command (BDS then answers "Unknown command" to `stop`).
- **Fresh world** per run: the world volume is created empty for each run.

## Docker Desktop and WSL2

Docker Desktop on Windows and macOS runs Linux containers in a lightweight VM. Its CPUs are virtual: `--cpuset-cpus`
pins virtual CPUs, and the host scheduler may move each one between physical cores, including efficiency cores. Such
runs are recorded with placement `vcpu` and the report prints a warning. Results from them are not comparable with
native runs, and "same pinning" there does not mean the same physical cores.

## Virtual machines and cloud instances

The same applies to any VM: placement is `vcpu`. Noisy neighbours on shared cloud hosts show up as run-to-run variance
that the foreign-load gate cannot see, because it only sees the guest. Use dedicated hosts for published results.
