"""Tests for the document-extract plugin (fork-plugins/document-extract): the pre_llm_call hook that
inlines attached documents' text from the gateway's context notes."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "fork-plugins" / "document-extract"


def _load_plugin():
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.document_extract", _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_plugin = _load_plugin()


def _note(display_name, path, *, text=False, inlined=False):
    """The exact shapes gateway/run.py::_build_document_context_note produces."""
    from gateway.run import _build_document_context_note

    mtype = "text/plain" if text else "application/octet-stream"
    return _build_document_context_note(display_name, path, mtype, content_inlined=inlined)


def test_pending_notes_are_found_in_strings_and_multimodal_parts(tmp_path):
    binary = _note("report.pdf", str(tmp_path / "report.pdf"))
    txt = _note("notes.txt", str(tmp_path / "notes.txt"), text=True)
    msg = f"{binary}\n\n{txt}\n\nplease summarize"
    assert _plugin.find_pending_documents(msg) == [
        ("report.pdf", str(tmp_path / "report.pdf")), ("notes.txt", str(tmp_path / "notes.txt")),
    ]
    parts = [{"type": "text", "text": binary}, {"type": "image_url", "image_url": {"url": "data:..."}}]
    assert _plugin.find_pending_documents(parts) == [("report.pdf", str(tmp_path / "report.pdf"))]


def test_already_inlined_text_notes_are_skipped(tmp_path):
    # Telegram/Discord inline small text files themselves; the hook must not send them twice.
    inlined = _note("notes.txt", str(tmp_path / "notes.txt"), text=True, inlined=True)
    assert _plugin.find_pending_documents(f"{inlined}\n\nhello") == []
    assert _plugin._on_pre_llm_call(user_message=f"{inlined}\n\nhello") is None


def test_hook_returns_extracted_text_as_context(tmp_path):
    src = tmp_path / "data.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    msg = f"{_note('data.csv', str(src), text=True)}\n\nwhat is in it?"
    out = _plugin._on_pre_llm_call(user_message=msg, session_id="s", platform="telegram")
    assert out and out["context"].startswith("[Auto-extracted text of 'data.csv':]")
    assert "a,b\n1,2" in out["context"]


def test_docx_goes_through_upstream_read_extract(tmp_path, monkeypatch):
    import tools.read_extract as rx

    doc = tmp_path / "memo.docx"
    doc.write_bytes(b"PK\x03\x04fake")
    monkeypatch.setattr(rx, "extract_document_text", lambda path: "memo body")
    out = _plugin._on_pre_llm_call(user_message=_note("memo.docx", str(doc)))
    assert out == {"context": "[Auto-extracted document text of 'memo.docx':]\nmemo body"}


def test_unextractable_or_missing_files_keep_the_note_only(tmp_path):
    missing = tmp_path / "gone.pdf"
    assert _plugin._on_pre_llm_call(user_message=_note("gone.pdf", str(missing))) is None
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"PK\x03\x04zip")
    assert _plugin._on_pre_llm_call(user_message=_note("bundle.zip", str(archive))) is None


def test_long_text_is_truncated_with_a_marker(tmp_path):
    big = tmp_path / "big.log"
    big.write_text("x" * (_plugin.MAX_CHARS + 500), encoding="utf-8")
    out = _plugin._on_pre_llm_call(user_message=_note("big.log", str(big), text=True))
    assert "(truncated)" in out["context"].splitlines()[0]
    assert len(out["context"]) < _plugin.MAX_CHARS + 200


def test_register_wires_pre_llm_call():
    class _Ctx:
        hooks = []

        def register_hook(self, name, cb):
            self.hooks.append((name, cb))

    ctx = _Ctx()
    _plugin.register(ctx)
    assert ctx.hooks == [("pre_llm_call", _plugin._on_pre_llm_call)]
