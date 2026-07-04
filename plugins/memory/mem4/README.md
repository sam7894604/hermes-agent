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

## Storage layout (under `$HERMES_HOME/mem4/`)

| Path | Purpose |
|---|---|
| `<code>.md` | L2/L3 microfiles (human-readable, git/Obsidian friendly) |
| `_mirror/<target>.md` | append-only mirror of built-in memory writes |
| `.mem4_state.json` | idempotent-init version marker |

## Deferred (not in this commit)

- **Feature ①** — `prefetch` / `sync_turn` / SQLite FTS5 recall + backfill from
  existing session history (`search()` and `_backfill()` are stubs; the
  `mem_search` tool is intentionally withheld until it is real).
- **Feature ④** — Dream consolidation via `on_session_end`.
- **Backends (a) remote-vault / (c) local-vault** — reserved topologies.
