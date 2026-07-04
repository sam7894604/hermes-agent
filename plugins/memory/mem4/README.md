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

## ① FTS5 recall (`mem_search`, `prefetch`, `sync_turn`)

mem4 owns its own SQLite FTS5 database (`$HERMES_HOME/mem4/recall.db`) that
indexes both conversation turns and the L2/L3 microfiles (design spike §10.8
decision B). It reuses the upstream `hermes_state.py` **dual-table** pattern so
Chinese search works:

- `docs_fts` (unicode61) for English/BM25 + `docs_fts_trigram` (trigram) for CJK.
- `_contains_cjk()` routes CJK queries to the trigram table; any CJK token
  shorter than 3 chars (trigram needs ≥3) falls back to a per-token LIKE scan.
- If the SQLite build lacks the trigram tokenizer, CJK queries degrade to LIKE —
  never a hard failure.
- Ranking layers a time-decay weight (half-life 30d) over relevance so recent
  material outranks equally-relevant older material.

Surfaces:

- **`mem_search(query, limit)` tool** — full-text recall of past turns / cold
  microfiles (English + Chinese).
- **`prefetch(query)`** — turn-start recall. **Local I/O only** (SQLite + files,
  never MCP/network — it runs synchronously on the hot path) and capped at
  `recall.prefetch_char_cap` characters (default 2000).
- **`sync_turn(...)`** — indexes each completed turn, filtered (min length, tool
  output stripped; the store dedups by content hash).

Backfill of existing history is resumable via the `.mem4_state.json`
`backfill_cursor` (design spike §10.4): a background worker indexes batches and
persists the cursor, so a restart resumes mid-stream; `mem_search` results carry
a `[backfill in progress]` note until it completes. A real deployment injects a
session-history source; without one, only microfiles/mirror are indexed.

### Rebuild — derived layers are always reconstructible

```
hermes mem4 rebuild
```

Clears and rebuilds the recall index from the source-of-truth files (microfiles
+ mirror logs), then re-runs history backfill. Non-destructive; never reads the
built-in memory files for writing. This is the fifth non-negotiable guarantee
(Fable 5 review §5): the recall index / derived layers can always be rebuilt.

## ② Auditor + A/B measurement

Value is measured with data, not estimates (design spike §7; Fable 5 §6).

**Auditor** (`audit.py`) — enable with `memory.mem4.audit.enabled: true`. Records
one JSONL line per recall/route/prefetch event to `$HERMES_HOME/mem4/audit.jsonl`
(query, hit/miss, route fts/trigram/like, injected chars, prefetch). A tool-call
miss is *precise*; the L0-hit rate (turns using no tool) is *estimated* offline.
`Auditor.export_to_baserow(writer, ...)` writes an **aggregate** row to Baserow
907 via an injected writer (never imports the MCP; tests pass a mock).

> **Baserow 907 schema note.** Table 907 (`memory_audit`) is aggregate-oriented
> (`type`, `entry_count`, `mem_chars`, `hot_hit_rate`, `est_tokens_saved`,
> `notes`). The aggregate export uses only those existing columns. Per-event
> logging would need new columns — see `audit.MISSING_907_FIELDS`. **Adding them
> is a schema change and is left to the operator** (the code does not alter 907).

**A/B arm** — `memory.mem4.arm: experiment|baseline`. In `baseline`, mem4 is
loaded but all agent-facing surfaces are off (no tools, no system-prompt legend,
no prefetch injection), so the hot-zone/tool surface matches pure built-in while
the recall store stays measurable. Running one A/B round:

1. Set `memory.mem4.arm: baseline` (or remove `memory.provider: mem4`
   entirely), run the workload, collect `audit.jsonl`.
2. Set `memory.mem4.arm: experiment`, run the same workload, collect again.
3. Compare grouped by `arm` (each event line carries its arm).

**QA harness** (`eval/harness.py`, fixture `eval/qa_fixture.json` — 24 items,
EN+ZH, exact + paraphrase). Deterministic recall eval with no model in the loop,
so it avoids the "B has extra tools" confound (Fable 5 §3). Run:

```
hermes mem4 eval
```

Reports recall accuracy (overall / en / zh / exact / paraphrase), route
distribution, injected chars, and the **gate** (design spike §7): SHIP if the
experiment recalls cold knowledge the baseline can't (Δ ≥ 30%), the resident hot
zone shrank, and net per-query chars improved — else ROLL BACK.

> Runs on **synthetic** fixture data. Real hit rates require deploying to
> toothless and collecting actual usage; the same harness + `audit.jsonl` then
> run against real data.

## Deferred

- **Backends (a) remote-vault / (c) local-vault** — reserved topologies.
- **Real-data measurement** — deploy to toothless + collect usage (operator-gated).
