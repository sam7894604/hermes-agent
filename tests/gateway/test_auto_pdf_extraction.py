"""Gateway-side automatic PDF extraction (GatewayRunner._auto_extract_pdf).

Reliable, model-independent PDF handling wired into the inbound pipeline:
  - text-layer PDFs are read with pymupdf and inlined into the message,
  - scanned PDFs (no text layer) fall back to render + the vision auxiliary,
  - non-PDF files are ignored (returns None),
  - pymupdf being unavailable degrades gracefully (returns None).

pymupdf is not a test-env dependency, so we inject a fake module.
"""

from __future__ import annotations

import asyncio
import sys
import types

from unittest.mock import AsyncMock

import pytest

from gateway.run import GatewayRunner


def _fake_pymupdf(pages_text):
    m = types.ModuleType("pymupdf")

    class _Page:
        def __init__(self, t):
            self._t = t

        def get_text(self):
            return self._t

    class _Doc:
        def __init__(self, texts):
            self._pages = [_Page(t) for t in texts]
            self.page_count = len(texts)

        def __iter__(self):
            return iter(self._pages)

        def __getitem__(self, i):
            return self._pages[i]

        def close(self):
            pass

    m.open = lambda path: _Doc(pages_text)
    return m


class _Self:
    """Minimal stand-in for GatewayRunner (methods only touch self._vision_...)."""


def test_non_pdf_returns_none(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"NOT-A-PDF-HEADER........")
    r = asyncio.run(GatewayRunner._auto_extract_pdf(_Self(), str(p), "x.bin"))
    assert r is None


def test_missing_file_returns_none(tmp_path):
    r = asyncio.run(GatewayRunner._auto_extract_pdf(_Self(), str(tmp_path / "nope.pdf"), "nope.pdf"))
    assert r is None


def test_text_layer_pdf_is_inlined(tmp_path, monkeypatch):
    p = tmp_path / "r.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"0" * 32)  # magic bytes ok; content irrelevant (fake pymupdf)
    monkeypatch.setitem(
        sys.modules, "pymupdf", _fake_pymupdf(["Aryaduta Bali\nTotal USD 336.35\nHSIN-YI LIU"])
    )
    r = asyncio.run(GatewayRunner._auto_extract_pdf(_Self(), str(p), "receipt.pdf"))
    assert r is not None
    assert "Auto-extracted text" in r
    assert "Aryaduta Bali" in r and "336.35" in r  # inlined verbatim, no model/tool needed


def test_scanned_pdf_falls_back_to_vision(tmp_path, monkeypatch):
    p = tmp_path / "s.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"0" * 32)
    monkeypatch.setitem(sys.modules, "pymupdf", _fake_pymupdf([""]))  # empty text -> scanned
    s = _Self()
    s._vision_read_scanned_pdf = AsyncMock(return_value="[Auto-read scanned PDF] Hotel Foo, TWD 10,826")
    r = asyncio.run(GatewayRunner._auto_extract_pdf(s, str(p), "scan.pdf"))
    assert r is not None and "Hotel Foo" in r
    s._vision_read_scanned_pdf.assert_awaited_once()


def test_pymupdf_unavailable_degrades(tmp_path, monkeypatch):
    p = tmp_path / "r.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"0" * 32)
    # Force `import pymupdf` to fail.
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    r = asyncio.run(GatewayRunner._auto_extract_pdf(_Self(), str(p), "r.pdf"))
    assert r is None


def test_vision_read_renders_and_transcribes(tmp_path, monkeypatch):
    # Fake a scanned doc + vision endpoint end-to-end for _vision_read_scanned_pdf.
    import gateway.run as run_mod

    class _Pix:
        def save(self, path):
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")

    class _Page:
        def get_pixmap(self, dpi=170):
            return _Pix()

    class _Doc:
        page_count = 1

        def __getitem__(self, i):
            return _Page()

    import tools.vision_tools as vt
    monkeypatch.setattr(
        vt, "vision_analyze_tool",
        AsyncMock(return_value='{"success": true, "analysis": "Aryaduta Bali  USD 336.35"}'),
    )
    r = asyncio.run(GatewayRunner._vision_read_scanned_pdf(_Self(), _Doc(), "scan.pdf", 8))
    assert r is not None
    assert "Aryaduta Bali" in r and "Page 1" in r
