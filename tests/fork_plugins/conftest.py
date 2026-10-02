"""Fixtures for the fork-plugins tests.

The platform override plugins subclass the bundled Discord/Telegram adapters, so their SDKs are mocked the
same way the gateway suite does: importing ``tests.gateway.conftest`` installs both mocks into
``sys.modules`` (module-level ``_ensure_*_mock()`` calls) before any test module imports an adapter.
"""
from tests.gateway.conftest import _ensure_discord_mock, _ensure_telegram_mock  # noqa: F401

_ensure_telegram_mock()
_ensure_discord_mock()
