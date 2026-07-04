"""mem4 — four-tier routed memory as a Hermes MemoryProvider (⑤-minimal chassis).

Wraps the L0/L1/L2/L3 routed-memory design (ADR-018) as a pluggable
``MemoryProvider`` in **coexist / augment** mode: it strengthens the built-in
``MEMORY.md``/``USER.md`` but never replaces them. Disabling it (removing
``memory.provider: mem4`` from config.yaml) degrades cleanly back to pure
built-in memory, with zero residue.

This module is the ⑤-minimal *chassis*. It implements:
  * provider identity + availability + idempotent one-time init (§10)
  * the ``mem_route`` tool (route code -> L2/L3 microfile read, with freshness
    tags and graceful miss handling)
  * a routing legend in the system prompt and pre-compression summary
  * mirroring of built-in memory writes into mem4-owned files (never the
    built-in files)
  * a switchable storage backend, defaulting to local-file (see backend.py)

Deferred (stubs / not wired) to keep this a single-feature commit:
  * feature ① — ``prefetch`` / ``sync_turn`` / FTS5 recall + backfill
  * feature ④ — Dream consolidation via ``on_session_end``

See design spike: 技術/架構決策/2026-07-04_四層記憶包裝為Hermes-Provider設計spike.md
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from .backend import (
    STATE_FILENAME,
    MIRROR_DIRNAME,
    LocalFileBackend,
    StorageBackend,
    build_backend,
    normalize_code,
)

logger = logging.getLogger(__name__)

#: State-marker schema version (design spike §10.1). Bump when the on-disk
#: layout changes; ``_migrate`` walks from the stored version up to this.
SCHEMA_VERSION = 1

#: Default backend when ``memory.mem4.backend`` is unset (design spike §9.3 b).
DEFAULT_BACKEND = "local-file"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

MEM_ROUTE_SCHEMA = {
    "name": "mem_route",
    "description": (
        "Read a mem4 cold-tier microfile by route code when built-in memory "
        "(the always-loaded MEMORY.md/USER.md) lacks the detail. Codes: "
        "sys (system/environment), fam (people/family), vlt (vault/knowledge), "
        "adr (architecture decisions), proto (protocols/workflows). The leading "
        "§ is optional. Returns the microfile content prefixed with a freshness "
        "tag; a miss falls back to built-in memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Route code, e.g. 'sys', 'fam', 'vlt', 'adr', 'proto'.",
            },
        },
        "required": ["code"],
    },
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class Mem4MemoryProvider(MemoryProvider):
    """Four-tier routed memory provider — ⑤-minimal chassis."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = dict(config) if config else None
        self._backend: Optional[StorageBackend] = None
        self._root: Optional[Path] = None
        self._session_id = ""
        self._platform = "cli"
        self._agent_context = "primary"
        self._active = False
        self._ran_migration = False
        self._state: Dict[str, Any] = {}

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "mem4"

    def _resolve_backend_kind(self) -> str:
        """Read ``memory.mem4.backend`` (default local-file). No network."""
        if self._config and self._config.get("backend"):
            return str(self._config["backend"])
        try:
            from hermes_cli.config import load_config

            config = load_config()
            memory = config.get("memory", {}) if isinstance(config, dict) else {}
            m4 = memory.get("mem4", {}) if isinstance(memory, dict) else {}
            if isinstance(m4, dict) and m4.get("backend"):
                return str(m4["backend"])
        except Exception:
            pass
        return DEFAULT_BACKEND

    def is_available(self) -> bool:
        """Ready if the configured backend is one ⑤-minimal implements.

        No network calls (design spike §9.2). ⑤-minimal only ships the
        local-file backend; a config pointing at an unimplemented remote/local
        vault returns False so the agent degrades to pure built-in memory
        rather than half-loading a broken provider.
        """
        return self._resolve_backend_kind() == "local-file"

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._agent_context = kwargs.get("agent_context", "primary")

        hermes_home = kwargs.get("hermes_home") or _default_hermes_home()
        self._root = Path(hermes_home) / "mem4"

        kind = self._resolve_backend_kind()
        self._backend = build_backend(kind, self._root)
        if self._backend is None:
            logger.warning("mem4: backend %r not implemented — provider inactive", kind)
            self._active = False
            return

        try:
            self._ran_migration = self._ensure_bootstrap()
            self._backfill()  # stub in ⑤-minimal (design spike §10.4 → feature ①)
            self._active = True
            logger.info(
                "mem4 active (backend=%s, microfiles=%d, migration_ran=%s)",
                kind, self._state.get("counts", {}).get("microfiles", 0),
                self._ran_migration,
            )
        except Exception as e:
            logger.warning("mem4 initialize failed — provider inactive: %s", e)
            self._active = False

    def shutdown(self) -> None:
        # No background threads in ⑤-minimal; nothing to flush.
        return

    # -- idempotent init / migration (design spike §10) ----------------------

    def _state_path(self) -> Path:
        assert self._root is not None
        return self._root / STATE_FILENAME

    def _read_state(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: Dict[str, Any]) -> None:
        assert self._root is not None
        self._root.mkdir(parents=True, exist_ok=True)
        self._state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _ensure_bootstrap(self) -> bool:
        """Idempotent one-time init guarded by a version marker (§10.1).

        Returns True if a migration ran on this call, False if the marker was
        already current (the common warm-start path — no rewrite).
        """
        assert self._root is not None
        self._root.mkdir(parents=True, exist_ok=True)
        state = self._read_state()
        current_v = int(state.get("schema_version", 0)) if state else 0
        if state and state.get("migration_complete") and current_v == SCHEMA_VERSION:
            self._state = state
            return False
        self._state = self._migrate(current_v, SCHEMA_VERSION, prior=state)
        return True

    def _migrate(self, from_v: int, to_v: int, *, prior: Dict[str, Any]) -> Dict[str, Any]:
        """Stepwise migration entry. ⑤-minimal implements only v0 -> v1.

        v0 -> v1: create the mem4 dir, ADOPT any existing microfiles (never
        rebuild — §10.2), reserve the backfill cursor (filled by feature ①),
        and write the marker. Non-destructive: only mem4-owned paths are
        created; the built-in memory files are never read-for-write here.
        """
        state = dict(prior or {})
        if from_v < 1 <= to_v:
            adopted = self._backend.list_codes() if self._backend else []
            # §10.6 verification: microfile count == actual .md count. Here the
            # adopted list *is* the on-disk enumeration, so it holds by
            # construction; we still record it for the audit trail.
            state.update(
                {
                    "schema_version": 1,
                    "migrated_at": _now_iso(),
                    "backfill_cursor": None,      # feature ① (§10.4)
                    "backfill_complete": False,   # FTS5 backfill deferred to ①
                    "counts": {"microfiles": len(adopted)},
                    "migration_complete": True,
                }
            )
        self._write_state(state)
        return state

    def _backfill(self) -> None:
        """FTS5 backfill from existing session history — DEFERRED to feature ①.

        Design spike §10.4 / §10.8 (decision B: mem4 owns its own FTS5 table).
        ⑤-minimal only reserves ``backfill_cursor`` in the marker; it indexes
        nothing. Kept as a named seam so ① fills it without reshaping the
        chassis.
        """
        return

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # ⑤-minimal exposes only the working tool. mem_search (conversation
        # recall) is intentionally withheld until feature ① backs it with FTS5
        # — advertising a dead tool would waste tokens and mislead the model.
        return [MEM_ROUTE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if tool_name == "mem_route":
            return self._tool_route(args or {})
        return tool_error(f"Unknown tool: {tool_name}")

    def _tool_route(self, args: dict) -> str:
        code = args.get("code", "")
        if not code:
            return tool_error("code is required")
        if not self._active or self._backend is None:
            return json.dumps(
                {"code": code, "found": False,
                 "result": "[mem4 inactive: built-in memory remains authoritative]"},
                ensure_ascii=False,
            )
        norm = normalize_code(code)
        if norm is None:
            return tool_error(f"invalid route code: {code!r}")
        result = self._backend.read_microfile(norm)
        if result is None:
            # Graceful miss: never an error — the built-in L0 is still there.
            return json.dumps(
                {"code": norm, "found": False,
                 "result": f"[mem4 miss: no microfile '{norm}' — built-in memory remains authoritative]"},
                ensure_ascii=False,
            )
        return json.dumps(
            {"code": norm, "found": True, "source": result.source,
             "stale": result.stale, "result": result.render()},
            ensure_ascii=False,
        )

    # -- system prompt / compression -----------------------------------------

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        # Deliberately tiny: do NOT re-inject L0 (built-in already loaded
        # MEMORY.md). Just the routing legend so the model knows mem_route
        # exists and what the codes mean (design spike §2).
        return (
            "# mem4 記憶路由（補強層）\n"
            "內建 MEMORY.md/USER.md 為權威 L0；mem4 提供按需的冷區微檔讀取。\n"
            "路由碼：§sys 系統/環境 · §fam 人物/家庭 · §vlt vault/知識 · "
            "§adr 架構決策 · §proto 協定/流程。\n"
            "L0 缺該細節時用 mem_route(code) 讀對應微檔"
            "（mem_search 對話召回於功能①上線後提供）。"
        )

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        # Feed the routing legend (and available codes) into the compression
        # summary so the map survives context compression (design spike §2 —
        # the direct benefit of ⑤). Free text only.
        if not self._active or self._backend is None:
            return ""
        legend = (
            "mem4 路由碼：§sys 系統 · §fam 人物 · §vlt 知識 · §adr 決策 · "
            "§proto 協定；用 mem_route(code) 按需讀冷區微檔。"
        )
        codes = self._backend.list_codes()
        if codes:
            legend += " 現有微檔：" + ", ".join(f"§{c}" for c in codes) + "。"
        return legend

    # -- built-in memory mirror (design spike §3 / §8.3) ---------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Observe built-in memory writes and mirror them into mem4-owned files.

        HARD INVARIANT: this only ever writes under ``$HERMES_HOME/mem4/`` — it
        never writes, moves, or deletes the built-in MEMORY.md/USER.md, which
        remain the sole source of truth. Removing the provider drops the mirror
        with zero effect on built-in memory.
        """
        if not self._active or self._root is None:
            return
        if action not in {"add", "replace"} or not content or not content.strip():
            return
        mirror_target = target if target in {"memory", "user"} else "memory"
        try:
            self._mirror_write(mirror_target, action, content)
        except Exception as e:
            logger.debug("mem4 mirror write failed (non-fatal): %s", e)

    def _mirror_write(self, target: str, action: str, content: str) -> None:
        assert self._root is not None
        mirror_dir = self._root / MIRROR_DIRNAME
        mirror_dir.mkdir(parents=True, exist_ok=True)
        path = mirror_dir / f"{target}.md"
        # Traversal guard: the write target must stay inside the mem4 mirror
        # dir. ``target`` is constrained to {"memory","user"} by the caller,
        # but assert the resolved path anyway so this can never escape to the
        # built-in memories directory.
        if path.resolve().parent != mirror_dir.resolve():
            raise ValueError(f"mem4 mirror target escaped root: {path!r}")
        entry = f"\n<!-- {_now_iso()} {action} -->\n{content.strip()}\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(entry)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register mem4 as a memory provider plugin."""
    ctx.register_memory_provider(Mem4MemoryProvider())
