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
| `document-extract` | hook (`pre_llm_call`) | inlines the text of attached PDFs/docs/text files that the gateway only saved to disk | `gateway/run_document_extract.py`, `gateway/run_inbound.py` |
| `cost-estimate` | dashboard | per-model cost over 1h/24h/7d/30d priced per tier, with a daily/monthly projection | `/api/analytics/cost-estimate` route, `SessionDB` aggregate, `RealtimeAnalytics.tsx` |
| `line-whitelist` | dashboard | LINE allowlist admin tab (users/groups/rooms, pending queue, records, name resolution); reads the store shipped by `platforms/line` | extra search tier in `hermes_cli/web_server_dashboard.py` |
| `platforms/line` | platform override | subclass of the bundled LINE adapter: config-backed whitelist + admin notification, `requires_mention` gating (quote-reply of the bot counts), passive group context + on-demand media backfill, display names, tables→bullets, and the `line_whitelist` agent tool | `plugins/platforms/line/{adapter,whitelist_store,whitelist_notify}.py`, `tools/line_whitelist_tool.py`, `toolsets.py`, `tools_config.py`, `delegate_tool_toolsets.py`, metrics schema |
| `platforms/discord` | platform override | subclass of the bundled Discord adapter: `? `/`?? ` auto-choice buttons after a reply, and the Approve/Ignore/Skip LINE whitelist card | `plugins/platforms/discord/adapter.py` (+ its README) |
| `platforms/telegram` | platform override | subclass of the bundled Telegram adapter: the Approve/Ignore/Skip LINE whitelist card (`linewl:` callbacks) | `plugins/platforms/telegram/adapter.py` |
| `groq-cf-stt` | transcription provider (`stt.provider: groq-cf`) | Groq Whisper through a Cloudflare AI Gateway: sends `cf-aig-authorization` from `CF_AIG_TOKEN` with an empty provider key (BYOK); direct `api.groq.com` behaves like the built-in `groq` backend | `tools/transcription_cloud.py` CF header hunk |

## Install on a host

```bash
fork-plugins/install.sh            # symlinks every entry into $HERMES_HOME/plugins and prints the enable commands
hermes plugins enable turbovault-fixups document-extract cost-estimate line-whitelist groq-cf-stt platforms/line platforms/discord platforms/telegram
hermes plugins list                # every entry must show as loaded — an override that fails to import takes its platform DOWN, not degraded
```

`install.sh` is idempotent; re-run it after adding a plugin here. Platform overrides go to
`$HERMES_HOME/plugins/platforms/<name>/` (same key as the bundled adapter).

### Platform overrides — how they work and what they cost

Each override imports the bundled adapter module (`plugins.platforms.<name>.adapter`), subclasses its
adapter class, and re-runs upstream's own `register()` through a delegating context that swaps only
`adapter_factory`. The platform entry (label, env names, install hint, limits, platform hint) is
therefore always upstream's; `tests/fork_plugins/test_platform_overrides_registration.py` asserts that
relationship against the live upstream module, so an upstream change to the registration shows up as a
test failure at the next sync instead of silent drift.

Unlike bundled platforms, which upstream registers *deferred* (the adapter is imported on first use), a
user platform plugin is loaded eagerly whenever plugins are discovered — including CLI start. Measured
on toothless (PM Python 3.14, SDKs installed, cold): plugin discovery alone imports in ~185 ms and
discovery plus the three adapters in ~640 ms, so the overrides add about 0.45 s to a `hermes` start that
runs plugin discovery (`hermes chat`, `hermes gateway`); the gateway imports them anyway, so there the
cost is nil. If that CLI latency ever matters, the override `register()` can be
rewritten to copy the scalar kwargs and proxy the callables lazily — the registration contract test
already covers that shape.

Behaviour differences versus the former core patches, all deliberate:

- `line_whitelist` is a **plugin toolset** (same name), and upstream turns a plugin toolset ON by default
  on every platform that has not recorded it in `known_plugin_toolsets.<platform>`
  (`hermes_cli/tools_config.py::_enabled_plugin_toolsets`; the static `_DEFAULT_OFF_TOOLSETS` and the
  platform-restriction table cannot name a plugin toolset). To keep it LINE-only, add `line_whitelist`
  to `known_plugin_toolsets.<platform>` for every other platform (or save that platform once in
  `hermes tools`, which records all plugin toolsets); keep it listed in `platform_toolsets.line`, as
  toothless already does. Where it does leak in, every call is still admin-gated by the handler.
- Delegated subagents **see** `line_whitelist` in their schema when the parent has it (the static
  `DELEGATE_BLOCKED_TOOLS` list cannot name a plugin tool) but every call from a delegated child is
  refused by the handler (`agent.delegation_context.is_delegated_child_process_context`).
- The metrics schema needs no `line_whitelist` entry: plugin toolsets collapse to `custom` in
  `hermes_cli/observability/shared_metrics_contract.py`.
- Discord auto-choice buttons are sent after `send()` returns success (a wrapper around upstream's
  `send`), never into a forum parent channel.
- `groq-cf-stt` is a *new* provider name (`stt.provider: groq-cf`, options under `stt.groq-cf`), because a
  plugin may not override the built-in `groq` backend; it reads `GROQ_BASE_URL` / `GROQ_API_KEY` /
  `CF_AIG_TOKEN` like the built-in one did in the fork.
- The Discord/Telegram cards and the `line-whitelist` dashboard reach `WhitelistStore` through the LINE
  plugin's module when it is loaded in the same process, else by importing
  `$HERMES_HOME/plugins/platforms/line/whitelist_store.py` by path — so the dashboard process works
  without loading platform plugins, and Discord/Telegram degrade to "Whitelist unavailable" when the
  LINE override is not installed.

## Tests

`tests/fork_plugins/` loads each plugin from this directory by path under its production module name
(`tests/fork_plugins/_plugin_loader.py` mirrors `PluginManager`'s `hermes_plugins.<slug>` naming) and,
where it matters, through real discovery against a temp `HERMES_HOME` whose `plugins/` holds a copy of
the entry. The Discord/Telegram SDK mocks come from `tests/gateway/conftest.py` via
`tests/fork_plugins/conftest.py`.
