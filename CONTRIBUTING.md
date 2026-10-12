# Contributing to bedrock-bench

Thanks for helping. This project is only useful if its numbers are trusted, so most of the rules below are about
keeping results comparable and auditable.

## Development setup

```shell
uv sync                         # Python 3.11+ with the dev group (pytest, ruff, jsonschema)
uv run pytest                   # unit + fake-server integration tests, no real server needed
uv run ruff check
uv run ruff format --check
uv run bbench docs metrics --check   # docs/METRICS.md matches the metric registry
```

Bots (Go 1.25+):

```shell
cd bots
go vet ./...
go test ./...
```

Tests never start a real Minecraft server and never download BDS. Real-server smokes run on maintainer machines
before each release.

## Ground rules for code

- **Only adapters know server names.** Phases, samplers, metrics and the report never branch on a server name. If a
  server needs different behaviour, add an adapter attribute or method and use it generically.
- **Servers are black boxes.** Adapters read console output and operating-system process data. No hooks, no injected
  code beyond the helper plugins in `servers/`, which only expose a tick counter, positions and similar read-outs.
- **Runtime dependencies:** the Python standard library and `psutil`. Anything else needs a strong case.
- **Metrics are pure functions** over the raw files of a run, so `bbench analyze` can recompute them byte for byte.
- **Every file carries an SPDX header** (`SPDX-License-Identifier: Apache-2.0`).
- **Comments are short.** Say what the code does when it is not obvious; leave the why for the docs or the PR.
- Python follows `ruff` (line length 120). Go follows `gofmt` and `go vet`.

## Changing a metric

1. Change the formula and its declaration in `src/bedrock_bench/metrics/registry.py`.
2. Bump `ANALYSIS_VERSION` in `src/bedrock_bench/__init__.py`.
3. Regenerate the golden fixtures under `tests/fixtures/runs/` and check the diff is what you expect.
4. Run `uv run bbench docs metrics` to regenerate `docs/METRICS.md`.
5. Add a CHANGELOG entry that names the metric and what changed.

A metric id carries its unit suffix (`_s`, `_ms`, `_mib`, `_cores`, `_ms_per_s`, `_frac`, `_per_bot_s`).

## Changing a schema

- Additive (new optional field, new enum value): bump `SCHEMA_MINOR`, update the schema and its example.
- Removing, renaming or redefining a field: bump `SCHEMA_VERSION`, keep the old schema file next to the new one and add
  a migration in `results.py`. See [docs/RESULTS_SCHEMA.md](docs/RESULTS_SCHEMA.md).

## Changing a scenario

A scenario's hash covers its fully resolved content and is stored in every run. Changing a published scenario makes
old and new results incomparable, so prefer a new scenario file. If you must change one, say so in the CHANGELOG.

## Adding or updating a server

Follow [docs/ADDING_A_SERVER.md](docs/ADDING_A_SERVER.md). The pull request needs:

- [ ] `src/bedrock_bench/adapters/<id>.py`, data-heavy and free of logic that belongs in the harness
- [ ] `servers/<id>/` templates and helper sources (no binaries, no worlds)
- [ ] `artifacts/<id>.toml` with url, size and sha256 for every platform
- [ ] `features/<id>.toml` with evidence for every cell (rules below)
- [ ] `docs/servers/<id>.md`: versions, setup, every override with category and reason, default-config failure modes
- [ ] fixture log lines in `tests/fixtures/logs/` (line shapes only, written by hand) and a unit test
- [ ] the adapter contract test passes (`uv run pytest tests/unit/test_adapters_contract.py`)
- [ ] `bbench smoke <id>@<version>` passes on every platform the adapter declares; attach the `run.json`
- [ ] every config change from the server's defaults is returned as an override with a category; `rescue` and
      `nonvanilla` overrides have a footnote that states what the default config does

## Feature-evidence rules

`features/<server>.toml` declares, per version range, how each feature compares with vanilla BDS
([docs/FEATURES.md](docs/FEATURES.md) is generated from these files).

- Every cell other than `unknown` cites evidence: a link to the server's source at a pinned commit or tag, its official
  documentation, or the metric that observes the feature (`observed_by`).
- Evidence must be public. Do not cite decompiled code, reverse-engineering notes, leaked material or anything under an
  NDA, for any server.
- `unknown` is allowed in the file but not in a published table for a key the scenario exercises. Find out, or leave
  that server out of the table.
- `altered` means a deliberate change that saves work (for example a config that skips some simulation). It needs a
  note saying what changes for players.
- When the declared class and the observed counter disagree, the report flags `parity_mismatch`. Fix the file or explain
  the counter in the PR.

## Publishing results

Results under `published/` are read by people deciding which server to run. A publication must satisfy all of these:

- Made with `bbench report --publish`, which enforces at least 3 `ok` runs per cell (or a cell that is all failures),
  released artifacts only (no `artifact = "<path>"` overrides), and no client-side generation scenario
  (`spread100-csg`) in a cross-server table.
- A clean harness checkout (`run.json` records `harness.dirty = false`) at a tagged or recorded commit.
- One machine profile per table. The report refuses mixed `machine.profile_sha256`.
- Parity rows and work counters next to every cost table, as the report lays them out. Do not crop them away when
  quoting.
- No ranking across fidelity sections and no composite score ([ADR 0002](docs/adr/0002-no-composite-score.md)).
- Raw run bundles attached to the GitHub release that matches the publication, with their sha256 listed in its
  `README.md`. Metrics and reports go in git. See [published/README.md](published/README.md).
- Published datasets are licensed CC BY 4.0.

Numbers from a profiled, patched or otherwise non-standard run are not results. Keep them out of `published/`.

## Mojang content

Never commit BDS binaries, worlds, world templates, resource packs or anything else from Mojang, and never upload them
as CI artifacts or release assets. `bbench fetch` downloads BDS on the user's machine after they accept the EULA.

## Commits and pull requests

- Keep pull requests focused. Separate a refactor from a behaviour change.
- Add a [CHANGELOG.md](CHANGELOG.md) entry under `Unreleased` for anything a user of the harness or a reader of results
  would notice: new servers, new metrics, formula changes, schema changes, scenario changes, fixed measurement errors.
- Describe how you tested. For harness changes that affect measurements, include before/after `metrics.json` from the
  same raw run (`bbench analyze`).

## Reporting a problem with a result

Open an issue with the run's `run.json`, `metrics.json` and `bbench doctor --json` output. If the server crashed,
attach the `crash/` folder. `run.json` contains no hostname, username or absolute path, but check any log you attach.
