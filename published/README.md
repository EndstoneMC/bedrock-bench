# Published results

This folder holds results that the project stands behind, and the provenance of earlier figures.

## Layout

```
published/
  <yyyy-mm-dd>-<name>/          one publication, for example 2026-11-02-endstone-0.11.14
    README.md                   what was measured, on which machine, harness commit, raw bundle links and sha256
    report.md                   the report as rendered by `bbench report --publish`
    summary.json                aggregate over repeats
    claims.json                 claim results, when the publication checks claims
    runs/<run_id>/run.json      provenance and status of every run, including failed and invalid ones
    runs/<run_id>/metrics.json  computed metrics of every run
  legacy-0.11.14/               provenance of Endstone 0.11.14's BENCHMARK.md (pre-harness)
```

Raw run bundles (logs and series, the rest of each run directory) are too large for git. They are attached as assets
to the GitHub release of the same name, one archive per matrix, and their sha256 is listed in the publication's
`README.md`. With them, anyone can run `bbench analyze` on the raw files and get the same `metrics.json` under the same
analysis version.

## Rules

- A publication is produced by `bbench report --publish`, which requires at least 3 `ok` runs per cell (or a cell that
  failed every time), released artifacts only, one machine profile per table, and the equal-chunk scenario for any
  cross-server table. See [CONTRIBUTING.md](../CONTRIBUTING.md#publishing-results).
- Every run of the matrix is included, whatever its status. Invalid runs are kept with their reason.
- The harness checkout must be clean and its commit recorded.
- Publications are immutable. A correction is a new publication that names the one it supersedes; the old folder gets
  a note at the top of its `README.md` and stays.
- Nothing from Mojang is included: no server binaries, worlds or world templates. From the server, raw bundles contain
  only its console output during the run and the configs the harness wrote.

## License

The datasets in this folder (reports, summaries, run and metrics files, and the raw bundles attached to releases) are
licensed under [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). Credit
"bedrock-bench contributors" and link to the publication.

## Legacy figures

[`legacy-0.11.14/`](legacy-0.11.14/) is not a publication under these rules. It archives the summaries behind Endstone
0.11.14's BENCHMARK.md, which came from single runs of the pre-harness scripts, and
[`PROVENANCE.md`](legacy-0.11.14/PROVENANCE.md) maps each published figure to its run, build and bot. Those figures are
never mixed into new tables.
