"""The LINE/Discord/Telegram override plugins register the bundled platform entry with upstream's own
kwargs and only the adapter factory swapped, and are discovered from ``$HERMES_HOME/plugins/platforms/
<name>/`` as user plugins that shadow the bundled adapters (``resolve_manifest_winners``)."""
from __future__ import annotations

import importlib
import shutil

import pytest

from gateway.config import PlatformConfig
from tests.fork_plugins._plugin_loader import FORK_PLUGINS, load_fork_plugin

CASES = [
    ("line", "LineAdapter", {"channel_access_token": "tok", "channel_secret": "sec"}),
    ("discord", "DiscordAdapter", {}),
    ("telegram", "TelegramAdapter", {}),
]


class _Recorder:
    """Stand-in PluginContext that records every registration it receives."""

    def __init__(self):
        self.platform_args = None
        self.platform = None
        self.tools = []
        self.other = []

    def register_platform(self, *args, **kwargs):
        self.platform_args = args
        self.platform = kwargs

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.other.append((name, args, kwargs))
        return _record


@pytest.mark.parametrize("name, cls_name, extra", CASES)
def test_register_reuses_upstreams_entry_with_the_override_adapter(name, cls_name, extra):
    plugin = load_fork_plugin(f"platforms/{name}")
    upstream = importlib.import_module(f"plugins.platforms.{name}.adapter")

    ours, theirs = _Recorder(), _Recorder()
    plugin.register(ours)
    upstream.register(theirs)

    assert theirs.platform_args == () and ours.platform_args == ()  # the override patches by keyword
    assert set(ours.platform) == set(theirs.platform)
    for key, value in theirs.platform.items():
        if key != "adapter_factory":
            assert ours.platform[key] == value, key
    assert ours.other == theirs.other

    cfg = PlatformConfig(enabled=True, token="t", extra=dict(extra))
    assert type(theirs.platform["adapter_factory"](cfg)) is getattr(upstream, cls_name)
    built = ours.platform["adapter_factory"](cfg)
    assert type(built) is getattr(plugin.adapter, cls_name)
    assert isinstance(built, getattr(upstream, cls_name))


def test_line_register_also_registers_the_whitelist_tool():
    plugin = load_fork_plugin("platforms/line")
    ours = _Recorder()
    plugin.register(ours)
    (tool,) = ours.tools
    assert tool["name"] == "line_whitelist" and tool["toolset"] == "line_whitelist"
    assert tool["schema"]["name"] == "line_whitelist"
    assert tool["check_fn"]() is True
    lwt = importlib.import_module("hermes_plugins.platforms__line.line_whitelist_tool")
    assert tool["handler"] is lwt._handle
    for other in ("discord", "telegram"):
        rec = _Recorder()
        load_fork_plugin(f"platforms/{other}").register(rec)
        assert rec.tools == []


@pytest.mark.parametrize("name, cls_name, extra", CASES)
def test_user_dir_override_shadows_the_bundled_platform_when_enabled(tmp_path, monkeypatch, name, cls_name, extra):
    import hermes_yaml as yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    shutil.copytree(FORK_PLUGINS / "platforms" / name, tmp_path / "plugins" / "platforms" / name)
    from hermes_cli import plugins as pmod
    from gateway.platform_registry import platform_registry

    key = f"platforms/{name}"
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins[key]
    assert loaded.manifest.source == "user" and not loaded.enabled  # opt-in, like every user plugin

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [key]}}))
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins[key]
    assert loaded.enabled, loaded.error
    assert loaded.manifest.source == "user" and not loaded.deferred

    entry = platform_registry.get(name)
    assert entry is not None and entry.plugin_name == f"{name}-platform"
    adapter = entry.adapter_factory(PlatformConfig(enabled=True, token="t", extra=dict(extra)))
    assert type(adapter).__name__ == cls_name
    assert type(adapter).__module__.startswith(f"hermes_plugins.platforms__{name}")
    upstream_cls = getattr(importlib.import_module(f"plugins.platforms.{name}.adapter"), cls_name)
    assert type(adapter) is not upstream_cls and isinstance(adapter, upstream_cls)
    if name == "line":
        assert "line_whitelist" in mgr._plugin_tool_names


class TestStockFallback:
    """Shadowing a bundled platform leaves no automatic fallback, so the override's entry point supplies
    one: when the override cannot import or its registration raises, upstream's stock adapter is
    registered and the failure is logged as an error."""

    @pytest.mark.parametrize("name, cls_name, extra", CASES)
    def test_import_failure_registers_the_stock_adapter(self, monkeypatch, caplog, name, cls_name, extra):
        plugin = load_fork_plugin(f"platforms/{name}")
        upstream = importlib.import_module(f"plugins.platforms.{name}.adapter")
        monkeypatch.setattr(plugin, "adapter", None)
        monkeypatch.setattr(plugin, "IMPORT_ERROR", ImportError("seam renamed upstream"))
        rec = _Recorder()
        with caplog.at_level("ERROR"):
            plugin.register(rec)
        built = rec.platform["adapter_factory"](PlatformConfig(enabled=True, token="t", extra=dict(extra)))
        assert type(built) is getattr(upstream, cls_name)  # stock, not the override subclass
        assert rec.tools == []
        assert any("STOCK" in r.getMessage() and "seam renamed upstream" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("name, cls_name, extra", CASES)
    def test_registration_error_falls_back_to_the_stock_adapter(self, monkeypatch, caplog, name, cls_name, extra):
        plugin = load_fork_plugin(f"platforms/{name}")
        upstream = importlib.import_module(f"plugins.platforms.{name}.adapter")

        def _boom(ctx):
            raise TypeError("register_platform() got an unexpected keyword")

        monkeypatch.setattr(plugin.adapter, "register", _boom)
        rec = _Recorder()
        with caplog.at_level("ERROR"):
            plugin.register(rec)
        built = rec.platform["adapter_factory"](PlatformConfig(enabled=True, token="t", extra=dict(extra)))
        assert type(built) is getattr(upstream, cls_name)
        assert any("registration raised" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize("name, cls_name, extra", CASES)
    def test_healthy_override_is_untouched_by_the_wrapper(self, name, cls_name, extra):
        plugin = load_fork_plugin(f"platforms/{name}")
        assert plugin.IMPORT_ERROR is None and plugin.adapter is not None
        rec = _Recorder()
        plugin.register(rec)
        built = rec.platform["adapter_factory"](PlatformConfig(enabled=True, token="t", extra=dict(extra)))
        assert type(built) is getattr(plugin.adapter, cls_name)
