## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- For changes that affect measurements, include before/after metrics.json from the same raw run (bbench analyze). -->

## Checklist

- [ ] `uv run ruff check`, `uv run ruff format --check` and `uv run pytest` pass.
- [ ] `CHANGELOG.md` has an entry under Unreleased if users or readers of results would notice the change.
- [ ] A metric formula change bumps `ANALYSIS_VERSION` and regenerates the golden fixtures and `docs/METRICS.md`.
- [ ] A schema change bumps `SCHEMA_MINOR`, or `SCHEMA_VERSION` with a migration in `results.py`.
- [ ] A scenario change is called out: its hash changes and old results stop being comparable.
- [ ] A new or updated server follows the adapter checklist in `CONTRIBUTING.md`.
- [ ] Nothing from Mojang, no absolute paths or hostnames, and no decompiled code or reverse-engineering notes.
