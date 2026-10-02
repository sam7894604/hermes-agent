"""LINE platform override plugin (fork-plugins/platforms/line): Markdown tables as bullets, the tracking
client swap at connect, and the ``line_whitelist`` tool wiring plus its delegated-child refusal.
Registration contracts live in test_platform_overrides_registration.py."""
from __future__ import annotations

import asyncio
import importlib
import json

from gateway.config import PlatformConfig
from tests.fork_plugins._plugin_loader import load_fork_plugin

_plugin = load_fork_plugin("platforms/line")
_line = _plugin.adapter
_lwt = importlib.import_module("hermes_plugins.platforms__line.line_whitelist_tool")


def _adapter():
    return _line.LineAdapter(PlatformConfig(
        enabled=True, extra={"channel_access_token": "tok", "channel_secret": "sec"}))


class TestTablesBecomeBullets:
    """LINE renders no table syntax — a GFM pipe table must not survive as literal "| a | b |" rows. The
    shared ``convert_table_to_bullets`` runs BEFORE upstream's stripping so a table inside a code fence
    stays verbatim."""

    def test_markdown_table_converted_to_bullets(self):
        md = (
            "Here:\n\n"
            "| Item | Cost |\n"
            "|------|------|\n"
            "| Coffee | 45000 |\n"
            "| Lunch | 541420 |\n"
        )
        out = _adapter().format_message(md)
        assert "|" not in out  # no raw pipes reach the bubble
        assert "Coffee" in out and "Lunch" in out
        assert "• Cost: 45000" in out
        assert "• Cost: 541420" in out

    def test_table_inside_code_fence_not_converted(self):
        md = "See:\n\n```\n| Item | Cost |\n|------|------|\n| Coffee | 45000 |\n```\n"
        out = _line.strip_markdown_preserving_urls(md)
        assert "| Coffee | 45000 |" in out  # kept as literal text
        assert "• Cost:" not in out

    def test_no_table_behaviour_matches_upstream(self):
        text = "**bold** and [link](https://x.com)\n- a"
        assert _line.strip_markdown_preserving_urls(text) == _line._base.strip_markdown_preserving_urls(text)
        assert _line.strip_markdown_preserving_urls("") == ""


class TestTrackingClient:
    def test_record_sent_keeps_the_bots_own_message_ids(self):
        client = _line._LineClient("tok")
        client._record_sent({"sentMessages": [{"id": "m1"}, {"id": "m2"}, {"quoteToken": "x"}]})
        client._record_sent(None)
        assert list(client.sent_message_ids) == ["m1", "m2"]
        assert client.sent_message_ids.maxlen == 500

    def test_connect_swaps_upstreams_client_for_the_tracking_one(self, monkeypatch):
        async def _upstream_connect(self, *, is_reconnect=False):
            self._client = _line._base._LineClient(self.channel_access_token)
            self._bot_user_id = "Ubot"
            return True

        monkeypatch.setattr(_line._base.LineAdapter, "connect", _upstream_connect)
        ad = _adapter()
        ad._mention_gate_warned = True
        assert asyncio.run(ad.connect()) is True
        assert isinstance(ad._client, _line._LineClient)
        assert ad._client._token == "tok"
        assert ad._mention_gate_warned is False  # fresh warning per connection cycle

    def test_failed_connect_leaves_the_client_alone(self, monkeypatch):
        async def _upstream_connect(self, *, is_reconnect=False):
            return False

        monkeypatch.setattr(_line._base.LineAdapter, "connect", _upstream_connect)
        ad = _adapter()
        assert asyncio.run(ad.connect()) is False
        assert ad._client is None


class TestWhitelistTool:
    def test_handler_routes_args_into_line_whitelist(self, monkeypatch):
        seen = {}

        def _fake(action, scope=None, id=None, note=None, pending_id=None, task_id=None):
            seen.update(action=action, scope=scope, id=id, note=note, pending_id=pending_id, task_id=task_id)
            return "{}"

        monkeypatch.setattr(_lwt, "line_whitelist", _fake)
        _lwt._handle({"action": "approve", "scope": "group", "id": "C1", "note": "n"}, task_id="t1", session_id="s")
        assert seen == {"action": "approve", "scope": "group", "id": "C1", "note": "n", "pending_id": None, "task_id": "t1"}

    def test_refuses_inside_a_delegated_child_before_touching_the_store(self, monkeypatch):
        from agent.delegation_context import delegated_child_context

        def _no_store():
            raise AssertionError("store must not be touched for a delegated child")

        monkeypatch.setattr(_lwt, "_get_store", _no_store)
        monkeypatch.setattr(_lwt, "_caller_user_id", lambda: "admin")
        with delegated_child_context():
            out = json.loads(_lwt.line_whitelist(action="list"))
        assert "delegated" in out["error"]
        assert out.get("success") is False
