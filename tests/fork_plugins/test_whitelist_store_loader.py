"""The Discord/Telegram overrides and the line-whitelist dashboard find ``WhitelistStore`` without the LINE
plugin loaded in-process: the path-import branch (``$HERMES_HOME/plugins/platforms/line/whitelist_store.py``)
is what the dashboard process and a LINE-less gateway actually take."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from tests.fork_plugins._plugin_loader import FORK_PLUGINS, load_fork_plugin

_DASHBOARD_API = FORK_PLUGINS / "line-whitelist" / "dashboard" / "plugin_api.py"


def _dashboard_loader():
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_line_whitelist_loader_test", _DASHBOARD_API)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return lambda: mod._whitelist_store_module().WhitelistStore


LOADERS = {
    "discord": lambda: load_fork_plugin("platforms/discord").adapter._whitelist_store_class,
    "telegram": lambda: load_fork_plugin("platforms/telegram").adapter._whitelist_store_class,
    "dashboard": _dashboard_loader,
}


@pytest.fixture
def no_line_plugin_in_process(monkeypatch):
    for name in [m for m in list(sys.modules) if m.startswith("hermes_plugins.platforms__line") or m == "hermes_fork_line_whitelist_store"]:
        monkeypatch.delitem(sys.modules, name, raising=False)


@pytest.mark.parametrize("consumer", sorted(LOADERS))
def test_path_import_from_the_installed_line_plugin(tmp_path, monkeypatch, no_line_plugin_in_process, consumer):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    loader = LOADERS[consumer]()

    with pytest.raises(Exception):
        loader()  # nothing installed yet

    shutil.copytree(FORK_PLUGINS / "platforms" / "line", tmp_path / "plugins" / "platforms" / "line")
    cls = loader()
    assert isinstance(cls, type) and cls.__name__ == "WhitelistStore"
    assert Path(sys.modules["hermes_fork_line_whitelist_store"].__file__) == tmp_path / "plugins" / "platforms" / "line" / "whitelist_store.py"
    assert loader() is cls  # cached: the file is not executed again


def test_loaded_line_plugin_wins_over_the_path_import(tmp_path, monkeypatch, no_line_plugin_in_process):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # nothing installed under HERMES_HOME
    line = load_fork_plugin("platforms/line")
    try:
        for consumer in ("discord", "telegram", "dashboard"):
            assert LOADERS[consumer]()() is line.whitelist_store.WhitelistStore
    finally:
        for name in [m for m in list(sys.modules) if m.startswith("hermes_plugins.platforms__line")]:
            sys.modules.pop(name, None)
