# fork-plugins — this fork's own features, kept out of the core tree

Everything under `fork-plugins/` is fork-only. upstream never ships this directory, so syncing with
upstream never conflicts here, and the core tree (`agent/`, `gateway/`, `tools/`, `hermes_cli/`,
bundled `plugins/`) can stay byte-identical to upstream.

They are **user plugins**: each entry is symlinked into `$HERMES_HOME/plugins/` and enabled through
`plugins.enabled`. A user plugin whose key matches a bundled one (`platforms/line`) shadows the
bundled copy — that is upstream's documented override mechanism (`hermes_cli/plugins_discovery.py::
resolve_manifest_winners`), which is how the LINE/Discord/Telegram customizations replace the stock
adapters without patching them.

| Plugin | Kind | What it does | Replaces (former core delta) |
|---|---|---|---|
| `turbovault-fixups` | hooks | repairs malformed `edit_note` SEARCH/REPLACE payloads; skips the doomed local `patch` after a vault write so the file-mutation verifier does not false-alarm | `tools/mcp_tool_handlers.py`, `agent/turn_{context,explainers,finalizer}.py` |
| `line-whitelist` | dashboard | LINE allowlist admin tab (users/groups/rooms, pending queue, records, name resolution) | extra search tier in `hermes_cli/web_server_dashboard.py` |

## Install on a host

```bash
fork-plugins/install.sh            # symlinks every entry into $HERMES_HOME/plugins and prints the enable commands
hermes plugins enable turbovault-fixups line-whitelist
```

`install.sh` is idempotent; re-run it after adding a plugin here. Platform overrides go to
`$HERMES_HOME/plugins/platforms/<name>/` (same key as the bundled adapter).

## Tests

`tests/fork_plugins/` loads each plugin from this directory by path (the way `PluginManager` names
user plugins) and, where it matters, through real discovery against a temp `HERMES_HOME` whose
`plugins/` holds a copy of the entry.
