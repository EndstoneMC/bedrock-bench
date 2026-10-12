# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Besides the harness version, entries name the versions that decide whether two results are comparable:

- **Schema:** `bedrock-bench/run/<major>`, `bedrock-bench/metrics/<major>` and `bedrock-bench/bot/<major>`, plus a minor
  number for additive changes.
- **Analysis:** `ANALYSIS_VERSION`, bumped whenever a metric formula changes.
- **Scenarios:** a change to a scenario file changes its hash; results with different hashes are not compared.

## [Unreleased]

### Added

- `bbench` command line: `doctor`, `fetch`, `bots`, `prepare-world`, `smoke`, `run`, `run-one`, `analyze`, `report`,
  `validate` and `docs`.
- Adapters for vanilla BDS, Endstone (from PyPI), LeviLamina, PocketMine-MP, Nukkit, PowerNukkitX and Dragonfly.
- Go load bot built from one source for every server, with a protocol 1001 build for PocketMine-MP.
- Scenarios `smoke`, `spread100`, `spread100-equal`, `spread100-pregen`, `chaos` and `chaos-portable`.
- Matrices for the Endstone 0.11.14 comparison, the cross-server comparison and LeviLamina 26.51.
- Metrics for tick rate, dip depth, recovery, tick-thread CPU per tick, process cores, memory, view-load latency and
  work counters, documented in `docs/METRICS.md`.
- Declared feature parity per server with evidence, shown next to every cost table.
- Validity gates and run statuses `ok`, `server_failed` and `invalid`, with exit codes derived from them.
- Result schemas `bedrock-bench/run/1`, `bedrock-bench/metrics/1` and `bedrock-bench/bot/1`; analysis version 1.
- Docker runtime for Linux servers, with the bots in the server's network namespace.
- Documentation: methodology, reading guide, fairness controls, workloads, pitfalls, platforms, results schema,
  profiling, reproduction guide for Endstone 0.11.14, and a guide to adding a server.
- Provenance of every figure in Endstone 0.11.14's BENCHMARK.md, with the legacy result summaries archived under
  `published/legacy-0.11.14/`.

[Unreleased]: https://github.com/EndstoneMC/bedrock-bench/commits/main
