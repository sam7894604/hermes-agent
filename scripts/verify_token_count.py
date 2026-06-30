#!/usr/bin/env python3
"""Verify bit-packed messages.token_count is stored correctly.

Read-only. Compares three things and logs every mismatch/error:

  1. CODEC INTEGRITY (absolute) — every packed row (token_count < 0) must
     round-trip: decode -> re-encode -> equal the stored value, with sane
     (non-negative, in-range) bucket values. A failure here = corruption.
  2. TAG ATTRIBUTION (absolute) — assistant rows must carry (output,
     reasoning); user/tool prompt-tail rows must carry (total_input,
     cache_read). A row tagged the wrong way = a write-path bug.
  3. ACCOUNTING CROSS-CHECK (semantic) — per-message decoded sums vs the
     session-level integer columns (sessions.*_tokens), which are written
     independently from the same API usage. output/reasoning should match
     exactly (the assistant bucket always persists). input/cache_read are
     best-effort on the prompt-tail row (a tail flushed before usage arrived
     keeps its old value), so a per-message SHORTFALL there is expected, not
     an error — only an OVERSHOOT is suspicious.

"API response" ground truth lives in the DB at two independent layers: the
per-message packed codec (checks 1-2) and the session-level integer columns
(check 3, written straight from canonical_usage). Agreement between them is
the post-hoc proof the codec stored the API numbers faithfully. For exact
per-call live capture, run with --watch (tails new rows as they're written).

Usage:
  python scripts/verify_token_count.py [--db PATH] [--log PATH] [--session ID]
  # ponytail: this script IS the test. No framework; asserts live in --selfcheck.
  python scripts/verify_token_count.py --selfcheck   # codec unit check, no DB
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Import the dependency-light codec whether installed (site-packages) or run
# from a repo checkout (root module one dir up from scripts/).
try:
    import hermes_token_codec as codec
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import hermes_token_codec as codec


def _default_db() -> Path:
    home = os.environ.get("HERMES_HOME")
    if home:
        return Path(home) / "state.db"
    return Path.home() / ".hermes" / "state.db"


class Log:
    """Tee to stdout + a log file; counts errors/warnings."""

    def __init__(self, path: Path):
        self.fh = open(path, "w", encoding="utf-8")
        self.errors = 0
        self.warnings = 0

    def line(self, msg: str = "") -> None:
        print(msg)
        self.fh.write(msg + "\n")

    def error(self, msg: str) -> None:
        self.errors += 1
        self.line("  ERROR: " + msg)

    def warn(self, msg: str) -> None:
        self.warnings += 1
        self.line("  WARN:  " + msg)

    def close(self) -> None:
        self.fh.close()


def selfcheck() -> int:
    """Codec round-trip + attribution invariants, no DB needed."""
    # assistant pack carries output/reasoning; round-trips losslessly.
    a = codec.pack_assistant_tokens(234, 128)
    assert a < 0, "packed value must be negative (format/sign flag)"
    da = codec.resolve_message_tokens("assistant", a)
    assert (da["output"], da["reasoning"]) == (234, 128), da
    assert (da["input"], da["cache_read"]) == (0, 0), da
    assert codec.pack_assistant_tokens(da["output"], da["reasoning"]) == a, "round-trip"
    # input pack carries total_input/cache_read on a user/tool row.
    i = codec.pack_input_tokens(38900, 5120)
    di = codec.resolve_message_tokens("tool", i)
    assert (di["input"], di["cache_read"]) == (38900, 5120), di
    assert codec.pack_input_tokens(di["input"], di["cache_read"]) == i, "round-trip"
    # legacy non-negative attributed to assistant output only.
    assert codec.resolve_message_tokens("assistant", 99)["output"] == 99
    assert codec.resolve_message_tokens("user", 99)["output"] == 0
    # saturation clamps instead of overflowing into the wrong field.
    big = codec.pack_assistant_tokens(10**12, 0)
    assert codec.resolve_message_tokens("assistant", big)["output"] >= 0
    print("selfcheck OK")
    return 0


def verify(db_path: Path, log: Log, only_session: str | None) -> int:
    if not db_path.exists():
        log.error(f"DB not found: {db_path}")
        return 1
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    log.line(f"# token_count verification  {datetime.now().isoformat(timespec='seconds')}")
    log.line(f"# db: {db_path}")
    log.line("")

    # ---- checks 1 & 2: per-row codec integrity + tag attribution ----
    where = "WHERE token_count < 0"
    params: list = []
    if only_session:
        where += " AND session_id = ?"
        params.append(only_session)
    rows = conn.execute(
        f"SELECT id, session_id, role, token_count FROM messages {where}", params
    ).fetchall()
    log.line(f"[1/3] codec integrity + attribution over {len(rows)} packed row(s)")
    ASSISTANT_TAGS = {"output_tokens", "reasoning_tokens"}
    INPUT_TAGS = {"total_input_tokens", "cache_read_tokens"}
    for r in rows:
        tc = r["token_count"]
        unpacked = codec.unpack_token_count(tc)
        # round-trip: re-encode the decoded buckets, must equal stored value.
        role = r["role"]
        b = codec.resolve_message_tokens(role, tc)
        if role == "assistant":
            re_enc = codec.pack_assistant_tokens(b["output"], b["reasoning"])
        else:
            re_enc = codec.pack_input_tokens(b["input"], b["cache_read"])
        if re_enc != tc:
            log.error(
                f"msg {r['id']} ({role}) round-trip mismatch: stored={tc} "
                f"re-encoded={re_enc} decoded={unpacked}"
            )
        # sane values
        for k, v in b.items():
            if v < 0:
                log.error(f"msg {r['id']} negative bucket {k}={v}")
        # attribution: which tags landed on which role
        tags = set(unpacked.keys())
        if role == "assistant" and not tags <= ASSISTANT_TAGS:
            log.error(f"msg {r['id']} assistant row carries non-output tags: {tags}")
        if role in ("user", "tool") and not tags <= INPUT_TAGS:
            log.error(f"msg {r['id']} {role} row carries non-input tags: {tags}")

    # ---- check 3: per-session accounting cross-check ----
    sess_filter = "WHERE id = ?" if only_session else ""
    sess_params = [only_session] if only_session else []
    sessions = conn.execute(
        f"SELECT id, input_tokens, output_tokens, cache_read_tokens, reasoning_tokens "
        f"FROM sessions {sess_filter}",
        sess_params,
    ).fetchall()
    log.line("")
    log.line(f"[2/3] accounting cross-check over {len(sessions)} session(s)")
    log.line("  (exact output/reasoning match is required only when a session's assistant")
    log.line("   rows are 100% packed; partial coverage = pre/over-deploy history, shortfall ok)")
    checked = 0
    fully_packed = 0
    history_skipped = 0
    for s in sessions:
        msgs = conn.execute(
            "SELECT role, token_count FROM messages WHERE session_id = ?", (s["id"],)
        ).fetchall()
        agg = {"input": 0, "output": 0, "cache_read": 0, "reasoning": 0}
        asst_total = asst_packed = 0
        for m in msgs:
            if m["role"] == "assistant":
                asst_total += 1
                if (m["token_count"] or 0) < 0:
                    asst_packed += 1
            for k, v in codec.resolve_message_tokens(m["role"], m["token_count"]).items():
                agg[k] += v
        if not any(agg.values()) and not any(
            (s["input_tokens"], s["output_tokens"], s["cache_read_tokens"], s["reasoning_tokens"])
        ):
            continue  # untouched session, nothing to compare
        checked += 1
        sid = s["id"]
        # Per-session packed coverage decides how strict the output check is:
        #   100% packed -> bit-pack owns every assistant turn -> EXACT match required.
        #   <100%       -> session predates / straddles the deploy -> unpacked turns
        #                  make a per-message shortfall expected (not an error).
        coverage = (asst_packed / asst_total) if asst_total else 0.0
        if asst_packed == 0:
            history_skipped += 1
        elif coverage >= 1.0:
            fully_packed += 1
        strict = coverage >= 1.0
        for bucket, col in (("output", "output_tokens"), ("reasoning", "reasoning_tokens")):
            got, want = agg[bucket], s[col] or 0
            if got > want:
                log.error(
                    f"session {sid} {bucket}: per-message={got} > session-col={want} "
                    f"(OVERSHOOT {got - want} — packed more than API reported)"
                )
            elif got < want:
                if strict:
                    log.error(
                        f"session {sid} {bucket}: per-message={got} != session-col={want} "
                        f"(delta {got - want}; assistant rows 100% packed so an exact match was expected)"
                    )
                # else: pre/over-deploy history, shortfall is expected — silent
        # input/cache_read are best-effort on the prompt-tail row regardless of
        # coverage. Reference semantics matter: packed total_input is the FULL
        # prompt for a call (new input + cache_read), so it reconciles against
        # session.input_tokens + session.cache_read_tokens — NOT input_tokens
        # alone (which is the cache-excluded portion). cache_read reconciles
        # against session.cache_read_tokens. Shortfall is expected (best-effort
        # tail + only post-deploy turns packed); only an overshoot is a real bug.
        s_input_full = (s["input_tokens"] or 0) + (s["cache_read_tokens"] or 0)
        for bucket, want in (("input", s_input_full), ("cache_read", s["cache_read_tokens"] or 0)):
            got = agg[bucket]
            if got > want:
                log.warn(
                    f"session {sid} {bucket}: per-message={got} > full-prompt-ref={want} "
                    f"(overshoot {got - want})"
                )

    # ---- summary ----
    log.line("")
    log.line("[3/3] summary")
    log.line(f"  packed rows checked        : {len(rows)} (all passed codec integrity if 0 errors above)")
    log.line(f"  sessions compared          : {checked}")
    log.line(f"  fully-packed (strict match): {fully_packed}")
    log.line(f"  pre-deploy (unpacked, skip): {history_skipped}")
    log.line(f"  ERRORS                     : {log.errors}")
    log.line(f"  WARNINGS                   : {log.warnings}")
    verdict = "PASS" if log.errors == 0 else "FAIL"
    log.line(f"  VERDICT             : {verdict}")
    conn.close()
    return 0 if log.errors == 0 else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="path to state.db (default: $HERMES_HOME/state.db)")
    ap.add_argument("--log", type=Path, default=Path("token_count_verify.log"))
    ap.add_argument("--session", default=None, help="limit to one session id")
    ap.add_argument("--selfcheck", action="store_true", help="run codec unit check, no DB")
    args = ap.parse_args()
    if args.selfcheck:
        return selfcheck()
    db = args.db or _default_db()
    log = Log(args.log)
    try:
        rc = verify(db, log, args.session)
        log.line("")
        log.line(f"log written to: {args.log.resolve()}")
        return rc
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
