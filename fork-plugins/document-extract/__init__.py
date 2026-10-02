"""document-extract — put attached documents' text in front of the model, automatically.

When a gateway message carries a document the inbound path prepends a context note such as
``[The user sent a document: 'report.pdf'. It is saved at: /path/report.pdf. Its text is not
inlined here ...]`` and leaves reading it to the model. A weak or distracted model never opens the
file. This ``pre_llm_call`` hook finds those notes in the turn's user message, extracts each file
with upstream's own document stack (``tools.read_extract``: DOCX/XLSX/IPYNB natively, PDF + the
PPTX / legacy Office / OpenDocument / RTF / EPUB family through anydoc; plain-text families read
directly) and returns the text as turn context. Notes whose content the adapter already inlined
(``Its content has been included below``) are skipped, so nothing is sent twice.

This used to be a gateway facade mixin (``gateway/run_document_extract.py``); as a plugin it needs
no core change. Best effort: a file that cannot be extracted keeps the path-pointing note.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

MAX_CHARS = 20_000
# The two "not inlined" shapes gateway/run.py::_build_document_context_note emits.
_NOTE_RE = re.compile(
    r"\[The user sent a (?:text )?document: '(?P<name>[^']*)'\. It is saved at: (?P<path>.+?)\. "
    r"Its (?:text|content) is not inlined here",
)
_TEXT_EXT = {
    ".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".py", ".sh", ".ts", ".tsv",
}


def _message_text(user_message: Any) -> str:
    """The textual part(s) of the turn's user message (a string, or multimodal content parts)."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts: List[str] = []
        for part in user_message:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def find_pending_documents(user_message: Any) -> List[tuple]:
    """``(display_name, path)`` for every attachment note whose content is not inlined yet."""
    return [(m.group("name"), m.group("path")) for m in _NOTE_RE.finditer(_message_text(user_message))]


def _wrap(kind: str, display_name: str, body: str) -> Optional[str]:
    body = (body or "").strip()
    if not body:
        return None
    trunc = " (truncated)" if len(body) > MAX_CHARS else ""
    return f"[Auto-extracted {kind} of '{display_name}'{trunc}:]\n{body[:MAX_CHARS]}"


def extract_document(path: str, display_name: str) -> Optional[str]:
    """Extracted text for one attachment, or ``None`` when this file is not extractable here."""
    try:
        with open(path, "rb") as f:
            head = f.read(5)
    except OSError:
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        if head == b"%PDF-" or ext == ".pdf":
            from tools.read_extract import extract_document_bytes

            with open(path, "rb") as f:
                pdf_bytes = f.read()
            pdf_name = display_name if display_name.lower().endswith(".pdf") else display_name + ".pdf"
            return _wrap("document text", display_name, extract_document_bytes(pdf_bytes, pdf_name))
        if ext in _TEXT_EXT:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return _wrap("text", display_name, f.read())
        from tools.read_extract import (
            ANYDOC_EXTENSIONS, EXTRACTABLE_EXTENSIONS, ExtractionError, extract_document_text,
        )
        if ext in (EXTRACTABLE_EXTENSIONS | ANYDOC_EXTENSIONS):
            try:
                return _wrap("document text", display_name, extract_document_text(path))
            except ExtractionError as exc:
                # Carries the "install anydoc" / NEEDS-OCR teaching text; the note still points at the file.
                logger.info("document-extract: cannot extract '%s': %s", display_name, exc)
                return None
    except Exception as exc:  # noqa: BLE001 — decoration must never break a turn
        logger.debug("document-extract failed for %s: %s", path, exc)
    return None


def _on_pre_llm_call(user_message: Any = None, **_: Any) -> Optional[dict]:
    blocks = []
    for display_name, path in find_pending_documents(user_message):
        extracted = extract_document(path, display_name)
        if extracted:
            blocks.append(extracted)
    if not blocks:
        return None
    return {"context": "\n\n".join(blocks)}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
