"""Inbound document extraction kept outside the gateway facade."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import List, Optional

logger = logging.getLogger("gateway.run")


class GatewayDocumentExtractMixin:
    async def _auto_extract_document(
        self, real_path: str, mtype: str, display_name: str
    ) -> Optional[str]:
        """Gateway-time extraction of an attached document's content so it
        reaches the agent inline — reliable and automatic, not left to the model
        to decide whether to call ``read_file``. Dispatches by type:

          * PDF          -> pymupdf (text) / render + vision for scanned pages
          * text family  -> read directly (txt/md/csv/json/xml/yaml/log/code…)
          * everything    -> ``tools.read_extract.extract_document_text``
            else            (DOCX/XLSX/IPYNB native; PPTX + legacy binary Office,
                             OpenDocument, RTF and EPUB via anydoc)

        The PDF branch stays local because ``read_extract`` has no fallback for a
        scanned page: without a hosted-OCR key it only warns, whereas the vision
        path here uses the model the operator already configured.

        Archives, video and unknown binaries return None and fall back to the
        path-pointing context note. Best-effort; never raises."""
        _MAX = 20000
        ext = os.path.splitext(real_path)[1].lower()
        try:
            with open(real_path, "rb") as _f:
                head = _f.read(5)
        except Exception:
            return None

        def _wrap(kind: str, body: str) -> Optional[str]:
            body = (body or "").strip()
            if not body:
                return None
            trunc = " (truncated)" if len(body) > _MAX else ""
            return f"[Auto-extracted {kind} of '{display_name}'{trunc}:]\n{body[:_MAX]}"

        try:
            # PDF (magic bytes also catch a legacy ".bin" whose name was lost).
            if head == b"%PDF-" or ext == ".pdf" or mtype == "application/pdf":
                return await self._auto_extract_pdf(real_path, display_name)

            _TEXT_EXT = {
                ".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml",
                ".toml", ".ini", ".cfg", ".py", ".sh", ".ts", ".tsv",
            }
            if mtype.startswith("text/") or ext in _TEXT_EXT:
                try:
                    with open(real_path, "r", encoding="utf-8", errors="replace") as _t:
                        return _wrap("text", _t.read())
                except Exception as exc:
                    logger.debug("Auto-doc: text read failed: %s", exc)
                    return None

            # Everything else upstream's extractor understands goes to that single
            # backend: DOCX / XLSX / IPYNB natively, and the PPTX + legacy binary
            # Office / OpenDocument / RTF / EPUB family through anydoc. The fork's
            # headless-LibreOffice bridge is gone -- anydoc covers the same formats
            # without a deployer-installed soffice, and one extraction stack beats
            # two that drift apart. PDFs never reach here (handled above, because
            # the vision fallback for scanned pages has no upstream equivalent).
            from tools.read_extract import (
                ANYDOC_EXTENSIONS, EXTRACTABLE_EXTENSIONS, ExtractionError,
                extract_document_text,
            )
            if ext in (EXTRACTABLE_EXTENSIONS | ANYDOC_EXTENSIONS):
                try:
                    body = await asyncio.to_thread(extract_document_text, real_path)
                except ExtractionError as exc:
                    # Carries the "install anydoc" teaching text for gated formats.
                    # The agent still gets the path-pointing context note.
                    logger.info("Auto-doc: cannot extract '%s': %s", display_name, exc)
                    return None
                except Exception as exc:
                    logger.debug("Auto-doc: read_extract failed for %s: %s", real_path, exc)
                    return None
                logger.info("Auto-doc: extracted '%s' via read_extract (%s)", display_name, ext)
                return _wrap("document text", body)
        except Exception as exc:
            logger.debug("Auto-doc extract failed for %s: %s", real_path, exc)
        # Unsupported here (archive/binary/unknown) -> context note.
        return None

    async def _auto_extract_pdf(self, real_path: str, display_name: str) -> Optional[str]:
        """Extract PDF content at gateway time so it reaches the agent inline —
        reliable and automatic, not left to the model to decide whether to run a
        tool. Text-layer PDFs are read with pymupdf (free, instant); a truly
        scanned PDF (no text layer) falls back to rendering each page and reading
        it with the vision auxiliary. Best-effort: returns an inlineable string,
        or None when the file isn't a PDF, pymupdf is unavailable, or it fails."""
        _MIN_CHARS, _MAX_CHARS, _MAX_PAGES = 24, 20000, 8
        # Confirm PDF by magic bytes — also catches legacy ".bin" caches whose
        # real filename was lost before the LINE filename fix.
        try:
            with open(real_path, "rb") as _f:
                if _f.read(5) != b"%PDF-":
                    return None
        except Exception:
            return None
        try:
            import pymupdf
        except Exception:
            logger.debug("Auto-PDF: pymupdf unavailable; skipping inline extraction")
            return None
        try:
            doc = pymupdf.open(real_path)
        except Exception as exc:
            logger.debug("Auto-PDF: open '%s' failed: %s", real_path, exc)
            return None
        try:
            full_text = "\n".join(pg.get_text() for pg in doc).strip()
            if len(full_text) >= _MIN_CHARS:
                trunc = " (truncated)" if len(full_text) > _MAX_CHARS else ""
                logger.info(
                    "Auto-PDF: inlined %d chars from text-layer PDF '%s' (%d page(s))",
                    len(full_text), display_name, doc.page_count,
                )
                return (
                    f"[Auto-extracted text of the attached PDF '{display_name}' "
                    f"({doc.page_count} page(s)){trunc}:]\n{full_text[:_MAX_CHARS]}"
                )
            logger.info(
                "Auto-PDF: '%s' has no text layer (scanned); vision-reading up to %d page(s)",
                display_name, min(doc.page_count, _MAX_PAGES),
            )
            return await self._vision_read_scanned_pdf(doc, display_name, _MAX_PAGES)
        finally:
            try:
                doc.close()
            except Exception:
                pass

    async def _vision_read_scanned_pdf(
        self, doc, display_name: str, max_pages: int
    ) -> Optional[str]:
        """Render each page of a scanned PDF and transcribe it with the vision
        auxiliary (whatever ``auxiliary.vision`` resolves to). Returns the joined
        transcription, or None if nothing could be read."""
        import uuid
        try:
            from tools.vision_tools import vision_analyze_tool
            from gateway.platforms.base import get_image_cache_dir
        except Exception as exc:
            logger.debug("Auto-PDF: vision deps unavailable: %s", exc)
            return None
        prompt = (
            "This is one page of a scanned document or receipt. Transcribe ALL "
            "readable text verbatim — names, dates, amounts, totals, line items — "
            "preserving numbers and currency exactly. If it is a table, keep the "
            "rows aligned."
        )
        pages: List[str] = []
        for i in range(min(doc.page_count, max_pages)):
            try:
                pix = doc[i].get_pixmap(dpi=170)
                png_path = str(
                    get_image_cache_dir() / f"pdfpage_{uuid.uuid4().hex[:12]}.png"
                )
                pix.save(png_path)
            except Exception as exc:
                logger.debug("Auto-PDF: render page %d failed: %s", i + 1, exc)
                continue
            try:
                res = await vision_analyze_tool(png_path, prompt)
                data = json.loads(res) if isinstance(res, str) else (res or {})
                analysis = (data.get("analysis") or "").strip() if isinstance(data, dict) else ""
                if isinstance(data, dict) and data.get("success") and analysis:
                    pages.append(f"--- Page {i + 1} ---\n{analysis}")
            except Exception as exc:
                logger.debug("Auto-PDF: vision page %d failed: %s", i + 1, exc)
        if not pages:
            return None
        extra = (
            ""
            if doc.page_count <= max_pages
            else f" (first {max_pages} of {doc.page_count} pages)"
        )
        logger.info(
            "Auto-PDF: vision-read %d page(s) of scanned '%s'", len(pages), display_name
        )
        return (
            f"[Auto-read scanned PDF '{display_name}'{extra} via vision:]\n"
            + "\n\n".join(pages)
        )

