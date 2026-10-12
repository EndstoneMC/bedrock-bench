# Pitfalls

Every trap below was hit at least once while building the benchmark behind Endstone's published 0.11.14 figures, or
was found while reviewing that work. Each entry says what went wrong and what the harness does about it now.

Status: **H** handled by design, **G** guarded by a validity gate (a run that hits it is invalid or failed),
**D** documented as a limitation or a server behaviour.

## Process and harness

- **A server death exited 0.** The legacy runner exited 0 even when the server died mid-run, so a script chaining
  runs treated a crash as success. Exit codes are now derived from the recorded run status; a run with a dead server
  cannot exit 0. *H*
- **Tracebacks after the server exits.** Samplers raised on a vanished process and the console writer wrote to a closed
  stdin, burying the real error. Samplers and the writer check liveness and stop cleanly; the death watch records the
  exit. *H*
- **Edits in place.** `server.properties` was edited in the server folder between runs, and arms shared one virtual
  environment, so one run could inherit another's config or files. Every run gets a fresh copy; installs and virtual
  environments are read-only once built. *H*
- **Reusing a tag deleted results.** Starting a run with an existing tag removed the previous results. Run ids are
  unique (`<UTC time>-<arm>-<scenario>-r<k>-<4 hex>`) and existing run directories are never touched. *H*
- **Shell chains.** Runs were sequenced by shell scripts waiting on marker files, with a `$?` that tested the wrong
  command, a GNU `date` flag that does not exist elsewhere, and a Python `False` printed and parsed as text. The runner
  is one Python program with no shell in between. *H*
- **Import hacks.** Scripts ran with `python -I`, which drops the script's own folder from `sys.path`, and patched the
  path back by hand. The harness is an installed package with a console entry point. *H*
- **Windows path length.** Deep server trees under a long scratch path exceeded the 260-character `MAX_PATH` limit.
  The cache uses short hashed directory names. Very deep custom trees can still hit it ([PLATFORMS.md](PLATFORMS.md)).
  *H, D*
- **Results only at the end.** An interrupted run lost everything. `run.partial.json` is written after every phase and
  raw series are streamed to disk. *H*
- **Endstone's launcher environment.** The Endstone launcher misbehaves with an inherited `PYTHONPATH`, `PYTHONHOME` or
  `VIRTUAL_ENV`, and it downloads BDS itself unless it finds its own version marker. The Endstone adapter scrubs the
  environment, sets `PYTHONSAFEPATH=1` and pre-seeds the verified BDS install with the marker. *H*
- **A silent re-download.** If the launcher replaces `bedrock_server` with a fresh download, the run measures an
  unverified binary. The adapter hashes the executable before and after the run. *G*
- **Lost executable bit.** Extracting the Linux BDS zip with Python's `zipfile` drops the executable bit, and BDS needs
  `LD_LIBRARY_PATH=.` to find its libraries. Extraction preserves file modes and the adapter sets the library path. *H*
- **Pinning after start.** Setting CPU affinity after the server started left thread pools sized for the whole machine.
  Windows servers start suspended and are pinned before they resume; Linux sets affinity before `exec`. *H*
- **Lost bot summary.** Killing the bot lost its final per-bot summary. The bot stops on `quit` from stdin, with
  `CTRL_BREAK` (Windows) or `SIGINT` (Linux) as fallback. *H*

## Time and sampling

- **Clock mix-ups.** Times parsed from log lines as hours:minutes:seconds broke at midnight; harness timers and log
  timestamps came from different clocks; containers logged in UTC. Everything is stamped with one monotonic clock at
  the moment the harness reads it, with one (monotonic, epoch) origin pair per run to map epoch-stamped sources. *H*
- **Stamped too early.** Samples taken through `docker exec` were stamped before the command ran, so their time was off
  by the exec latency. The Docker sampler runs as a sidecar and stamps each sample after reading it. *H*
- **A hard-coded CPU frequency.** Thread cycle counts were converted to seconds with a fixed 2.1 GHz, wrong on any other
  CPU. Thread CPU time now comes from Windows thread times and Linux `schedstat` (nanoseconds). Cycle counts are an
  optional secondary column with a calibrated frequency. *H*
- **Clock ticks assumed to be 100 Hz.** Linux `/proc` CPU times are in clock ticks. The harness reads
  `sysconf(SC_CLK_TCK)` instead of assuming 100. *H*
- **Thread names with spaces and parentheses.** A thread named `REST Thread(0)` shifts every field of
  `/proc/<pid>/task/<tid>/stat` when split naively. The parser starts after the last `)`. *H*
- **Born and exited threads dropped.** Threads created or ended inside a window were left out of per-thread deltas.
  Threads created inside a window count from zero, exited threads keep their last value, and process CPU comes from
  process counters, which include exited threads. *H*
- **Memory at one instant.** Memory was read once at the end of a window. It is sampled every second and reported as
  mean and maximum per window. *H*
- **The monitor used a core.** Scanning every process back to back cost one CPU core on the bot side. Per-CPU busy time
  is read every second; process attribution runs every 10 s; the harness's own CPU is reported. *H*
- **"Other load" included the server.** The machine-load figure counted the server itself as foreign. Foreign load is
  machine busy time minus the server, bot and harness process trees. *H*

## TPS and metric validity

- **TPS above 20.** TPS between two probe replies a few seconds apart includes the replies' own jitter, so single
  samples read above 20 or dip spuriously. Values are never clamped, and nothing shorter than a 5 s span is published on
  its own. *H, D*
- **Probe stuck behind commands.** Under console backlog the tick probe waited behind queued commands, so its reply
  time no longer matched the tick. The probe goes through a priority lane and its latency is gated (p99 at most
  1000 ms in steady). *H, G*
- **Percentiles from seven samples.** The join phase holds about 7 probe samples; a "5th percentile" of 7 is the
  minimum. p5 is omitted when there are fewer than 20 samples. *H*
- **A diluted settle median.** The median TPS over settle depends on how long settle lasts, and settle has a 300 s
  floor. Dip metrics replace it: lowest 5 s and 15 s TPS, ticks lost, time below 19.5 and recovery time. *H*
- **"Main thread ms per tick" is not MSPT.** The legacy figure divided tick-thread CPU by TPS and was read as
  milliseconds per tick. It excludes time the thread spends blocked. It is now named tick-thread CPU per tick
  ([ADR 0003](adr/0003-tick-cpu-is-not-mspt.md)). *H*
- **Saturation hides cost.** Once the tick thread is saturated, CPU per second stops growing while the work keeps
  growing. The report shows tick-thread CPU per executed tick, the saturated fraction of seconds, and real time per tick
  (`mspt_saturated`) when TPS is below 19.5. *H, D*
- **Coarse view-load times.** Time to 90% of a bot's view was read from bot summaries printed every 60 s, so it was
  quantised to 60 s, and nudges acted on data up to a minute old. The bot now records when it first saw each chunk and
  reports progress every 5 s. *H*
- **Counting the wrong chunks.** Chunks streamed for a bot's old position counted as part of its view; chunks were
  counted without their dimension, so Nether and overworld chunks at the same coordinates collided; and the spawn
  placeholder position was reported as a real one. Chunks are keyed by (dimension, x, z), only chunks within the
  current view count, and placeholder positions are never reported. *H*
- **Final view after chaos.** "Final unique chunks" was taken after chaos teleports had inflated it. The final view is
  taken at `settled`. *H*
- **Resends counted as delivery.** Raw chunk packet counts include resends. Unique chunks are counted, with the resend
  ratio reported separately. *H*
- **A hard-coded "view never filled".** Whether a view filled was judged against a fixed chunk count. View fill is
  measured against the view disc for the scenario's distance (317 columns at 10), and `views_incomplete` flags runs
  where many bots lag the median. *H*
- **Join time mixed clocks.** Join time subtracted a harness timestamp from a log timestamp at different points of the
  login. It is now measured on the bot's clock from dial to spawn, and is not a headline metric because it ends before
  any chunk arrives. *H, D*
- **Bot shortcuts that change the work.** The bot never acknowledges cached chunks before spawning and reports every
  cached chunk as a miss, so servers always send full chunks. This is a documented worst case applied to every
  server. *D*
- **Single runs.** Every legacy figure came from one run, with tick-thread noise around ±15 ms per second. Results need
  repeats and a difference is printed only when every run separates. *H*
- **Two "lowest TPS" figures.** The legacy table's "lowest TPS while views load" was the lowest 5 s value, while the
  text quoted the lowest 15 s value. Both are now reported under their own names (`tps_load_low5`, `tps_load_low15`).
  *H, D*

## Bots

- **Bots that never moved.** A bot that does not send the loading-screen-end packet after spawning stays, as far as the
  server is concerned, on its loading screen: its movement input is ignored, it never falls, it gets no corrections and
  no new chunks as it "walks". Every bot run before 10 October 2026 was frozen this way, and spread-player capacity was
  overstated as a result. The bot sends it by default and the smoke scenario checks that positions change and
  corrections appear. *H, G, D*
- **Clustered bots.** Bots walking near each other share chunks and flatter the server. Bots are spread over a grid
  with 40 chunks between neighbours, and the spread is validated. *H, G*
- **`spreadplayers` into ungenerated terrain.** It placed bots at unpredictable heights. Bots get protective effects and
  `tp <bot> x 300 z`, then fall once their chunks arrive. *H*
- **Correction cost.** A bot claiming flat walking over real terrain receives about one movement correction per second
  from servers that validate movement. That is work a real client would not cause. It is counted and reported as
  work. *D, H*
- **Swallowed decode errors.** The bot library logged packets it could not decode and carried on. Decode errors are
  counted, reported as events, and make the run invalid. *G*
- **Identical bots.** Every bot started facing the same direction, and identities were random per run. Start direction
  follows the golden angle by bot index; UUIDs are derived from a seed and the bot name. *H*
- **Sub-chunk requests off by one.** The legacy bot requested one sub-chunk too many, starting from the overworld
  minimum in every dimension. Requests now cover exactly the dimension's height range. *H*
- **An unattributed library snapshot.** The bot built against a vendored, modified copy of its protocol library,
  including a patch nothing used. The new bot pins upstream in `go.mod`; each remaining patch is listed in
  `bots/PATCHES.md` with a failing test and an upstream link. *H*
- **Different bots per server.** Each server family had its own bot binary and flags. One source and one flag set now
  serve every server, with a small allowlist of compatibility flags ([ADR 0001](adr/0001-one-bot-source.md)). *H*
- **No retry, short deadline.** The first join was never retried and had a 60 s deadline, so a slow server lost bots
  that would have joined. The deadline is 180 s, retries are declared in the scenario, and the outcome is reported as
  joined/n. *G*
- **Teleporting before the player is in the world.** Dragonfly reported a connection before the player entered the
  world, so teleports and effects went nowhere. The harness waits for the adapter's "player in world" line and
  re-teleports bots whose teleport was not confirmed. *H*
- **Lost console commands.** Commands written faster than a server's console reads them were dropped. Commands go
  through a paced writer, teleports are confirmed by the bot, and re-teleports are counted. *H*
- **Nudging airborne bots.** Some bots hover above terrain that never loads. Nudges are bounded (2 per bot), settle-only
  and counted. *H, D*
- **Per-IP flood guards.** All bots come from 127.0.0.1, which trips per-IP packet limits in some servers: PocketMine-MP
  blocked the address for 300 s and every session timed out at once, which looked like a chunk-sending stall. Each
  adapter raises the limit (helper plugin or config) as a `harness` override, and any dropped bot is reported. *H, G*
- **Shared UUIDs on Nukkit and PowerNukkitX.** They derive the player UUID from the XUID, and offline bots have none, so
  every bot got the same UUID and kicked the previous one. Those adapters add the allowlisted `-fake-xuid` flag. *H*
- **Nukkit kicks hovering bots.** Nukkit kicks players that fly when flight is off. Its adapter enables `allow-flight`.
  *H*
- **PocketMine-MP speaks an older protocol.** `pocketmine@5.44.3` supports protocol 1001 only, and its login needs
  fields that the matching library version did not send. A separate frontend built from the same core behaviour handles
  it, with the login back-port as a documented patch; such runs are flagged `bot_protocol_differs`. *H*
- **Hovering bots on servers without physics.** On servers without player physics the bots stay at the teleport height.
  The y distribution is reported so this is visible. *D, H*
- **Mangled text in pipes.** The section sign `§` and other non-ASCII text broke when passed through console pipes in
  the system code page. Pipes are UTF-8 and bot output is JSON. *H*

## Server and config parity

- **Client-side chunk generation.** With it, BDS sends about 100 chunks per player at view distance 10; without it,
  about 450, a 4.4-fold difference in chunk work. BDS does not use it on flat, legacy or void worlds even when the
  property is on, and older Endstone launchers turned it off. Every ranking scenario turns it off on every server,
  Endstone included; `spread100-csg` keeps it on only to reproduce the legacy figures; the `csg_matches` gate checks
  the effective setting. *H, G*
- **World generation differs.** Each server family generates different terrain from the same seed, or none at all. The
  world type is recorded, `spread100-pregen` separates delivery from generation, and the parity rows show the
  generator. *H, D*
- **World formats are not interchangeable.** A world written by one server family is regenerated, mis-decoded or
  silently replaced by flat terrain in another. PowerNukkitX only generates chunks that a player is viewing. Templates
  are built per server by its own pipeline (a bot tour by default) and validated by per-cell coverage. *H, G*
- **Nukkit's watchdog.** With default settings Nukkit took about 2 minutes per spawn under 100 joining bots, and its
  first autosave stalled the main thread long enough for the watchdog to shut the server down. That is a
  `server_failed` outcome; the `nukkit@b3caad6+no-watchdog` variant is a `rescue` override with a footnote. *H, D*
- **PocketMine-MP's memory limit.** The default 1 GiB main-thread memory limit crashed PocketMine-MP about 23 s into a
  100-bot join. Setting the limits to 0 is a `rescue` override with a footnote. Crash upload and the updater are
  turned off. *H, D*
- **JVM default heap.** Without `-Xmx` a JVM takes a quarter of the machine's RAM (16 GB on a 64 GB machine). Every JVM
  server gets the same fixed heap. *H*
- **Divergent defaults.** PowerNukkitX loads every level and uses its own difficulty default; Dragonfly does not save
  data by default. These are aligned as `parity` overrides. *H*
- **Auto tick rate.** Nukkit and PowerNukkitX can tick a slow level less often while its time still advances, which
  hides slowness from a time-based probe. It is turned off, and the probe must be a per-tick counter. *H, G*
- **Tick probes that are not tick counters.** World time and day time can stop or jump, and some servers print no
  number at all. Adapters declare a monotonic per-tick counter (with a helper command where needed), and the counter
  must advance during idle. *H, G*
- **The tick thread differs.** Endstone renames the BDS main thread to `Main`; Linux BDS has four threads named
  `MC_SERVER` (the tick thread has the lowest id); Go servers have no single tick thread; PowerNukkitX has other hot
  threads besides `main`. Each adapter declares a tick-thread rule, and cross-server tables use process cores. *H*
- **First-start prompts and ports.** Nukkit asks for a language on first start, PocketMine-MP runs a setup wizard,
  servers default to port 19132, and PocketMine-MP also binds IPv6. Adapters answer prompts, disable wizards, use the
  machine profile's port and turn IPv6 off. *H*
- **Thread pools ignore the CPU set.** PocketMine-MP's `async-workers: auto` counts every logical CPU, not the ones the
  process may use. The number of worker threads BDS starts does not change with the affinity mask either, nor with a
  container cpuset on Linux, so it differs between machines with the same CPU set. The thread census at idle records
  pool sizes; PocketMine-MP's workers are set to the server CPU count as a `parity` override. *H, D*
- **Server behaviour that changes the work.** These are properties of the servers, not of the harness, and are
  documented where they affect a number:
  - on vanilla BDS under this load, only a few of the chunk-generation worker threads do measurable work during the
    join, which per-thread CPU shows;
  - PowerNukkitX loads the chunks it ticks synchronously from disk on its main thread;
  - vanilla BDS on Linux merges some pairs of players in its locator-bar bookkeeping, so its locator traffic differs
    from vanilla BDS on Windows. *D*
- **Allocators.** Endstone preloads mimalloc on Linux and LeviLamina replaces the allocator on Windows; both trade
  memory for speed. The allocator is recorded as a fact, and variants can switch it where the server allows. *H, D*
- **LeviLamina's BDS build.** `levilamina@26.51.6` runs on `bds@1.26.51.1`, not the build vanilla runs on elsewhere.
  It is compared with `bds@1.26.51.1`. *H*
- **LeviOptimize rewrites its config.** It merges its config on upgrade and saves it on shutdown, so a second run would
  inherit changes. The pinned config is copied into every run. *H*
- **LeviLamina's extra threads.** It adds about 28 threads. They are grouped in the diagnostic thread table. *H*
- **LeviOptimize options that change gameplay.** Enabling all LeviOptimize options changes simulation. That variant
  (`levilamina@26.51.6+levioptimize-all`) is `nonvanilla` with fidelity V2. *H*
- **`endstone@0.11.13`'s DevTools thread.** A DevTools thread spins at about 0.7 of a core in `endstone@0.11.13` (fixed
  in `endstone@0.11.14`). It is footnoted as a `known_issue`; its CPU is not subtracted. *D*
- **Which build is running.** The legacy Windows runs swapped Endstone's runtime library into a PyPI install of
  `endstone@0.11.13`, so every arm printed the same version banner. Endstone is installed as a whole wheel from PyPI
  per version, and the banner must match the arm. *H, G*
- **Error counts that mean different things.** A broad "ERR" pattern counted harmless lines; BDS prints a multi-line
  `TRANSPORT TYPE ERROR` banner at every start; LeviLamina uses three-letter levels; the Endstone launcher logs its own
  crash handling as errors. Each adapter maps its levels and lists benign lines, and error counts are never compared
  across servers. *H*
- **Parsing positions.** `querytarget` prints multi-line JSON behind a log prefix on every line, and a failed parse was
  read as zero players. The prefix is stripped per line and the JSON found by bracket matching; unknown positions make
  the run invalid. *H, G*

## Chaos

- **Skipping actions under backlog.** The legacy chaos driver skipped actions when the console fell behind, so busier
  servers got a lighter test. Nothing is skipped; every action records scheduled, sent and applied times, and a lag over
  60 s stops new actions and flags `chaos_overrun`. *H*
- **Regimes by queue time.** Tick-speed regimes were split by when the command was queued; under backlog it applied up
  to 2.5 minutes later, which moved TPS samples into the wrong regime (17.9 instead of 19.4 in one run). Regimes come
  from the server's own "applied" lines, and samples that straddle a change are excluded. *H*
- **Commands that did nothing.** `gamerule playerwaypoints` takes `off` or `everyone`, not `true`/`false`, so the toggle
  silently failed; `locatorbar` is not a valid command; `status` exists only on Endstone. Dialects were fixed, and every
  action is checked by the contract test. *H*
- **Unreproducible plans.** Chaos used an unset seed and picked targets from whoever was online at the moment. The plan
  is generated before the run from a seed and targets bot indices. *H*
- **Dialects that drop commands.** Servers without an equivalent command silently ignored some actions. Unsupported
  actions are counted, never sent, and `chaos-portable` uses only actions every adapter supports. *H*
- **Extreme tick speeds.** Random tick speeds of 100 and 1000 dominate the cost, and some servers cannot run them at
  all. Cross-server chaos figures use normal tick speed only. *H*
- **Short monitor windows.** The legacy chaos monitor's TPS samples spanned about 12 s, short enough for probe jitter to
  matter. Low chaos figures use spans of at least 15 s. *H*
- **Jitter from the start.** The legacy Linux chaos bot jittered its view radius from the moment it joined, not from
  chaos start, so its settle phase differed from Windows. Jitter starts at chaos start only. *H, D*

## Machine

- **Hard-coded CPU numbers.** Server and bot CPU sets were written for one hybrid CPU. Topology is detected, and `auto`
  picks the performance cores. *H*
- **WSL2 pins virtual CPUs.** `--cpuset-cpus` under WSL2 or Docker Desktop pins virtual CPUs, which the host may run on
  any core, so "same pinning" there is not physical pinning. Placement is recorded as `vcpu` with a warning. *G, D*
- **Docker's UDP proxy.** Bots connecting through Docker Desktop's published UDP port received about a tenth of the
  native chunk traffic, so the server looked healthy because it was underloaded. Bots run in the server container's
  network namespace and connect to 127.0.0.1; `doctor` compares a Docker smoke with native. *H, G*
- **A shared machine.** Other benchmarks, compilers, browsers or a running Minecraft client competed for the CPUs; one
  chaos run overlapped another benchmark and read 17.2 TPS instead of 19.3. The machine lock, the idle gate and the
  per-window foreign-load gate make such runs invalid. *G*
- **Diagnostics inside measured windows.** A sampling profiler cost about 6% TPS at saturation and its trace files made
  antivirus scan gigabytes during the run; a fixed 12 s pause sat before the join; Java thread dumps ran during steady.
  No diagnostics run in measured windows ([PROFILING.md](PROFILING.md)). *H*
- **Bots on the same host.** Bots share the machine with the server. They are pinned to separate CPUs and a run with
  saturated bot CPUs is invalid. Shared caches and memory bandwidth remain a documented limitation. *G, D*
- **Missing costs.** The bots skip encryption, and mob-free flat worlds hide mob cost. Both are listed as limitations;
  an entity-dense scenario is planned. *D*

## Provenance and documentation

- **Development builds behind published rows.** Some published figures came from development builds, with no commit or
  image digest recorded next to them. Every run now records artifact hashes, the install-tree hash, the harness commit
  and the bot build; [PROVENANCE.md](../published/legacy-0.11.14/PROVENANCE.md) maps each legacy figure to its build.
  *H, D*
- **MiB divided by 1000.** Memory in MiB was divided by 1000 and printed as GB. Memory is reported in MiB, or GiB as
  MiB / 1024. *H*
- **Wording that overstated.** The legacy write-up said the bots joined "at the same time" (they joined 300 ms apart)
  and that Linux used "the same pinning" (it pinned virtual CPUs). The corrections are listed in
  [REPRODUCING-endstone-0.11.14.md](REPRODUCING-endstone-0.11.14.md). *D*
- **Evidence from one server's own test suite.** Endstone's equivalence build and its in-server tests support Endstone's
  correctness claims, but they cannot be applied to other servers. The harness relies on declared parity with evidence
  and on observed counters instead. *H*
