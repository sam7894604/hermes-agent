"""Groq Whisper speech-to-text through a Cloudflare AI Gateway — a transcription-provider plugin.

The built-in ``groq`` STT backend talks to ``GROQ_BASE_URL`` with ``GROQ_API_KEY`` and nothing
else. Routed through a Cloudflare AI Gateway (BYOK), the gateway rejects every request without a
``cf-aig-authorization`` header (401 AiGatewayError, code 2009). This provider sends that header
from ``CF_AIG_TOKEN`` and, as in BYOK mode the gateway supplies the stored Groq key, sends an empty
provider key. A direct ``api.groq.com`` base URL behaves exactly like the built-in backend.

Select it with ``stt.provider: groq-cf``; ``stt.groq-cf.{model,language,prompt,timeout,max_retries}``
mirror the built-in sections (``stt.language`` is the shared fallback). Registered via
``ctx.register_transcription_provider``; the result envelope is the one every STT backend returns
(``success``/``transcript``/``provider``/``error``).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

PROVIDER_NAME = "groq-cf"
DEFAULT_MODEL = "whisper-large-v3-turbo"
MODELS = (
    {"id": "whisper-large-v3-turbo", "display": "Whisper large-v3 turbo (Groq)"},
    {"id": "whisper-large-v3", "display": "Whisper large-v3 (Groq)"},
)
# Models that only exist on OpenAI's own endpoint; the built-in groq backend auto-corrects them too.
OPENAI_ONLY_MODELS = frozenset({"whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe", "gpt-transcribe"})
DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
CF_GATEWAY_HOST = "gateway.ai.cloudflare.com"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 1


def _env(name: str) -> str:
    """Secret/env lookup through Hermes' profile-aware reader (``.env`` + scoped secrets)."""
    try:
        from hermes_cli.config import get_env_value
        value = get_env_value(name)
    except Exception:
        value = os.getenv(name)
    return (value or "").strip()


def _provider_section() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        stt = load_config().get("stt") or {}
        section = stt.get(PROVIDER_NAME) if isinstance(stt, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _number(section: Dict[str, Any], key: str, default, cast=float):
    try:
        return cast(section[key]) if section.get(key) not in (None, "") else default
    except (TypeError, ValueError):
        return default


def transport() -> Dict[str, Any]:
    """``{base_url, api_key, default_headers}`` for the Groq OpenAI-compatible client.

    Through the Cloudflare gateway: the ``cf-aig-authorization`` bearer and an EMPTY provider key
    (BYOK — the gateway holds the Groq key). Direct Groq: the key, no extra headers."""
    base_url = _env("GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL
    api_key = _env("GROQ_API_KEY")
    via_gateway = CF_GATEWAY_HOST in base_url
    token = _env("CF_AIG_TOKEN") if via_gateway else ""
    headers = {"cf-aig-authorization": f"Bearer {token}"} if token else {}
    return {"base_url": base_url, "api_key": "" if token else api_key, "default_headers": headers,
            "via_gateway": via_gateway, "has_credentials": bool(api_key or token)}


def _transcript_text(transcription: Any) -> str:
    """``response_format="text"`` answers with a string; tolerate SDK objects/dicts, and surface a
    structured error instead of its repr."""
    if isinstance(transcription, str):
        return transcription.strip()
    is_mapping = isinstance(transcription, dict)
    value = transcription.get("text") if is_mapping else getattr(transcription, "text", None)
    if isinstance(value, str):
        return value.strip()
    if is_mapping or hasattr(transcription, "text"):
        error = transcription.get("error") if is_mapping else getattr(transcription, "error", None)
        raise ValueError(str(error) if error else "Transcription response contained no text")
    return str(transcription).strip()


class GroqCloudflareTranscriptionProvider(TranscriptionProvider):
    """Groq Whisper via an OpenAI-compatible client, with the Cloudflare gateway header when routed."""

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Groq Whisper (Cloudflare AI Gateway)"

    def is_available(self) -> bool:
        try:
            import openai  # noqa: F401
        except Exception:
            return False
        return transport()["has_credentials"]

    def list_models(self) -> List[Dict[str, Any]]:
        return [dict(m) for m in MODELS]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def transcribe(
        self, file_path: str, *, model: Optional[str] = None, language: Optional[str] = None, **extra: Any,
    ) -> Dict[str, Any]:
        t = transport()
        if not t["has_credentials"]:
            return self._error("GROQ_API_KEY not set" + (" (and no CF_AIG_TOKEN for the Cloudflare gateway)"
                                                           if t["via_gateway"] else ""))
        try:
            from openai import OpenAI
        except Exception:
            return self._error("openai package not installed")
        section = _provider_section()
        model_name = (model or section.get("model") or DEFAULT_MODEL).strip()
        if model_name in OPENAI_ONLY_MODELS:  # auto-correct an OpenAI-only model, like the built-in backend
            logger.info("Model %s not available on Groq, using %s", model_name, DEFAULT_MODEL)
            model_name = DEFAULT_MODEL
        language = language or (section.get("language") or None)
        prompt = extra.get("prompt") or section.get("prompt") or None
        create_kwargs = {k: v for k, v in (("language", language), ("prompt", prompt)) if v}
        client = None
        try:
            client = OpenAI(
                api_key=t["api_key"], base_url=t["base_url"], default_headers=t["default_headers"] or None,
                timeout=_number(section, "timeout", DEFAULT_TIMEOUT),
                max_retries=_number(section, "max_retries", DEFAULT_MAX_RETRIES, cast=int),
            )
            with open(file_path, "rb") as audio_file:
                transcription = client.audio.transcriptions.create(
                    file=audio_file, model=model_name, response_format="text", **create_kwargs)
            text = _transcript_text(transcription)
            logger.info("Transcribed %s via Groq%s (%s, lang=%s, %d chars)", Path(file_path).name,
                        " through Cloudflare AI Gateway" if t["via_gateway"] else "", model_name,
                        language or "auto", len(text))
            return {"success": True, "transcript": text, "provider": PROVIDER_NAME}
        except Exception as exc:
            logger.warning("Groq transcription failed: %s", exc)
            return self._error(f"Groq transcription failed: {exc}")
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    @staticmethod
    def _error(message: str) -> Dict[str, Any]:
        return {"success": False, "transcript": "", "provider": PROVIDER_NAME, "error": message}


def register(ctx) -> None:
    ctx.register_transcription_provider(GroqCloudflareTranscriptionProvider())
