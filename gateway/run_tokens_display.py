"""Profile-owned gateway token display preferences."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from hermes_constants import get_hermes_home
from gateway.config import Platform

logger = logging.getLogger("gateway.run")


class GatewayTokensDisplayMixin:
    @property
    def _TOKENS_DISPLAY_PATH(self) -> Path:
        return self.__dict__.get("_tokens_path_override") or get_hermes_home() / "gateway_tokens_display.json"

    @_TOKENS_DISPLAY_PATH.setter
    def _TOKENS_DISPLAY_PATH(self, path: Path) -> None:
        self._tokens_path_override = path

    def _tokens_state(self) -> dict:
        states = self.__dict__.setdefault("_tokens_states", {})
        key = str(self._TOKENS_DISPLAY_PATH)
        if key not in states:
            states[key] = {"global": False, "chats": {}}
            states[key]["chats"] = self._load_tokens_display()
        return states[key]

    @property
    def _tokens_display(self) -> Dict[str, bool]:
        return self._tokens_state()["chats"]

    @_tokens_display.setter
    def _tokens_display(self, chats: Dict[str, bool]) -> None:
        self._tokens_state()["chats"] = chats

    @property
    def _tokens_display_global(self) -> bool:
        return self._tokens_state()["global"]

    @_tokens_display_global.setter
    def _tokens_display_global(self, enabled: bool) -> None:
        self._tokens_state()["global"] = enabled

    def _tokens_key(self, platform: Platform, chat_id: str) -> str:
        """Platform-namespaced key for the per-session /tokens override."""
        return f"{platform.value}:{chat_id}"

    def _load_tokens_display(self) -> Dict[str, bool]:
        """Load per-session overrides; also sets ``_tokens_display_global``.

        Format: ``{"global": bool, "chats": {"<platform>:<chat>": bool}}``.
        ``global`` is the ``/tokens always`` preference (all conversations);
        ``chats`` are per-session ``on``/``off`` overrides that win over it.
        A legacy flat ``{key: bool}`` file is migrated as per-session overrides.
        """
        self._tokens_display_global = False
        try:
            data = json.loads(self._TOKENS_DISPLAY_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        if "chats" in data or "global" in data:
            self._tokens_display_global = bool(data.get("global", False))
            chats = data.get("chats") or {}
        else:
            chats = data  # legacy flat format
        return {
            str(k): bool(v)
            for k, v in chats.items()
            if isinstance(k, str) and ":" in str(k)
        }

    def _save_tokens_display(self) -> None:
        try:
            self._TOKENS_DISPLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._TOKENS_DISPLAY_PATH.write_text(
                json.dumps(
                    {
                        "global": bool(getattr(self, "_tokens_display_global", False)),
                        "chats": self._tokens_display,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("Failed to save tokens display state: %s", e)

    def _tokens_enabled_for(self, platform: Platform, chat_id: str) -> bool:
        """Effective /tokens state: a per-session override wins over global."""
        key = self._tokens_key(platform, chat_id)
        if key in self._tokens_display:
            return self._tokens_display[key]
        return bool(getattr(self, "_tokens_display_global", False))

