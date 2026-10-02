"""Load a ``fork-plugins/`` entry the way ``PluginManager`` does in production.

A directory plugin is imported as ``hermes_plugins.<slug>`` with the plugin dir as its package path
(``hermes_cli/plugins_loader.py::_load_directory_module``), so relative imports inside the plugin
resolve and the Discord/Telegram overrides find the LINE plugin's ``whitelist_store`` under its
production name in ``sys.modules``. ``slug`` is the manifest key with ``/`` → ``__`` and ``-`` → ``_``
(``platforms/line`` → ``platforms__line``).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

FORK_PLUGINS = Path(__file__).resolve().parents[2] / "fork-plugins"
_NS = "hermes_plugins"


def production_module_name(key: str) -> str:
    return f"{_NS}.{key.replace('/', '__').replace('-', '_')}"


def load_fork_plugin(key: str) -> types.ModuleType:
    """Import ``fork-plugins/<key>`` as ``hermes_plugins.<slug>``; repeat calls return the cached module."""
    name = production_module_name(key)
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    if _NS not in sys.modules:
        ns = types.ModuleType(_NS)
        ns.__path__ = []  # type: ignore[attr-defined]
        ns.__package__ = _NS
        sys.modules[_NS] = ns
    plugin_dir = FORK_PLUGINS / key
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        raise FileNotFoundError(f"fork plugin not found: {init_file}")
    spec = importlib.util.spec_from_file_location(name, init_file, submodule_search_locations=[str(plugin_dir)])
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {init_file}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        for stale in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            sys.modules.pop(stale, None)
        raise
    return module
