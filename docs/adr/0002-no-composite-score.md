# 0002. No composite score

- Status: accepted
- Date: 2026-10-12

## Context

Readers want one number per server. Any such number has to weigh tick rate against CPU, memory, view-load time and the
features a server implements. The weights encode a use case: a lobby server and a survival server care about different
things. And servers do different amounts of work for the same bots. A server without player physics, mob AI or a
terrain generator is cheaper because it skips work, and a single score would rank that as better engineering.

## Decision

- The report never computes a composite score, and never ranks arms of different fidelity against each other.
- Arms that are `vanilla` on every feature the scenario exercises go in a "Full vanilla fidelity" section; the rest go
  in "Reduced fidelity", each with a "does less than baseline" list. Sections are sorted by arm id.
- Every cost table sits next to a work table and the parity rows (rule R10).
- Efficiency metrics (CPU per player-tick, ticks per CPU second, chunks per CPU second) are printed only next to the
  parity rows they depend on, and are comparable only between arms with equal parity on the scenario's `requires` list.
- Pairwise comparisons use the complete-separation rule: "A better than B" only when every run of A beats every run of
  B, otherwise `~`.

## Consequences

- Reports are longer and need reading. [READING.md](../READING.md) explains how.
- Third parties can still build a ranking from `summary.json`, but not from this project's reports, and published
  tables cannot be cropped into one without dropping the parity rows the publishing rules require.
- Adding a server never reorders the others.
