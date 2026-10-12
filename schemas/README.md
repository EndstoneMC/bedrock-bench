# Schemas

JSON Schema (draft 2020-12) for every file the harness writes or reads as data.

| File | Validates | Id |
|---|---|---|
| `run.schema.json` | `run.json` and `run.partial.json` of one run | `bedrock-bench/run/1` |
| `metrics.schema.json` | `metrics.json` of one run | `bedrock-bench/metrics/1` |
| `bot-event.schema.json` | one line of the bot's JSONL output (`bots.jsonl.gz`) | `v = 1` |
| `claims.schema.json` | `claims/*.toml` after TOML parsing | `schema = 1` |

`examples/` holds one valid instance of each; `tests/unit/test_schemas.py` checks them.

Versioning: an additive change (a new optional field, a new enum value) bumps `SCHEMA_MINOR` in
`src/bedrock_bench/__init__.py`. Removing, renaming or redefining a field bumps the major: the old schema file stays
next to the new one and `results.py` migrates old runs. Metric formulas are versioned separately by `ANALYSIS_VERSION`.
