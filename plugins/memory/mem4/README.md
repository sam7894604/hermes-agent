# mem4 — four-tier routed memory provider (⑤-minimal chassis)

Wraps the L0/L1/L2/L3 routed-memory design (ADR-018) as a Hermes
`MemoryProvider` in **coexist / augment** mode. It strengthens the built-in
`MEMORY.md` / `USER.md` — it never replaces them, and never writes them back.
Disabling it degrades cleanly to pure built-in memory with zero residue.

Design spike: `技術/架構決策/2026-07-04_四層記憶包裝為Hermes-Provider設計spike.md`.

## Enable

```yaml
# config.yaml
memory:
  provider: mem4
  mem4:
    backend: local-file   # default; the only backend in the ⑤-minimal chassis
    dream:
      enabled: true       # ④ Dream consolidation (default on; set false to cede)
      threshold: 25       # new memory writes before an event-triggered consolidation
      staleness_days: 7   # consolidate at a session boundary if overdue by this
```

Remove `memory.provider: mem4` to disable → falls back to built-in memory.

## What this chassis does (⑤-minimal)

- **`mem_route(code)` tool** — read an L2/L3 cold-tier microfile by route code
  (`sys` / `fam` / `vlt` / `adr` / `proto`; leading `§` optional) from
  `$HERMES_HOME/mem4/<code>.md`. Every read is prefixed with a freshness tag
  (`[fresh: local-file]`, `[STALE: …]`, `[built-in only]`); a miss falls back to
  built-in memory rather than erroring.
- **Routing legend** in the system prompt and pre-compression summary (does not
  re-inject L0).
- **Mirror** of built-in memory writes into `$HERMES_HOME/mem4/_mirror/<target>.md`
  — mem4-owned files only; the built-in memory files are never touched.
- **Idempotent one-time init** with a version marker
  (`$HERMES_HOME/mem4/.mem4_state.json`) — adopts existing microfiles, never
  rebuilds, non-destructive, verified before completing (design spike §10).
- **Switchable storage backend** (`backend.py`) — `read_microfile` /
  `write_microfile` / `search`, defaulting to local-file.

## ④ Dream consolidation (in-provider, no external cron)

Dream runs entirely inside the provider as pure code — it does **not** depend on
or coordinate with any external cron. Triggers:

- **Event / threshold** — each built-in memory write is a signal; crossing
  `dream.threshold` new writes triggers a consolidation.
- **Staleness floor** — at a session boundary (start via `initialize`, end via
  `on_session_end`), if it has been longer than `dream.staleness_days` since the
  last consolidation **and** there is pending signal, consolidate.
- **Idle skip** — no pending signal ⇒ nothing to consolidate ⇒ skip. Pure idle
  (no sessions) needs no timer. *Known non-goal:* idle-time periodic
  consolidation (a future optional external scheduler hook could add it).

v1 consolidation compacts the mem4-owned mirror logs (dedup), **archiving the
pre-compaction original to `_mirror/_archive/` before rewriting** so nothing is
lost. It **only touches mem4-owned L2/L3** — never the built-in hot zone. A
marker + lock make the event and staleness paths mutually exclusive. Setting
`dream.enabled: false` makes Dream a complete no-op (and cedes to an official
background-memory agent, should Hermes ship one — upstream issue #553).

### Deployment note — retire the standalone Dream cron

If a deployment previously ran Dream as a **user-set cron / `jobs.json` entry**
(e.g. on toothless), that entry becomes **redundant once mem4 ships with ④** and
should be **retired/disabled** to avoid double-running against the same L2/L3.
This is a deployment step (retire the cron entry — no cron code change), applied
when mem4+④ is deployed; it is not part of this plugin.

## Storage layout (under `$HERMES_HOME/mem4/`)

| Path | Purpose |
|---|---|
| `<code>.md` | L2/L3 microfiles (human-readable, git/Obsidian friendly) |
| `_mirror/<target>.md` | append-only mirror of built-in memory writes |
| `_mirror/_archive/<target>-<ts>.md` | pre-consolidation originals (④ Dream, non-destructive) |
| `.mem4_state.json` | idempotent-init version marker |
| `.dream_state.json` | ④ Dream state: last consolidation, signal count |
| `.dream.lock` | ④ Dream mutual-exclusion lock (transient) |

## Deferred

- **Feature ①** — `prefetch` / `sync_turn` / SQLite FTS5 recall + backfill from
  existing session history (`search()` and `_backfill()` are stubs; the
  `mem_search` tool is intentionally withheld until it is real). Per the Fable 5
  spike review, ① must reuse the upstream `hermes_state.py` dual-FTS5-table
  pattern (unicode61 + trigram + CJK routing + LIKE fallback) so Chinese recall
  works, and `prefetch()` must be local-I/O-only with a char cap (no MCP /
  network — it runs synchronously on the turn's hot path).
- **Backends (a) remote-vault / (c) local-vault** — reserved topologies.
