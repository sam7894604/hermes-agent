"""Discord platform override — auto-detected choice buttons + the LINE whitelist decision card.

Installed at ``$HERMES_HOME/plugins/platforms/discord/`` it shadows the bundled ``platforms/discord``
plugin. ``DiscordAdapter`` subclasses upstream's ``plugins.platforms.discord.adapter`` and overrides two
seams; ``register()`` re-runs upstream's registration with this adapter so the platform entry stays
upstream's.

* **Auto-choice buttons.** When an agent reply opts in with a ``? `` (single-select) or ``?? ``
  (multi-select) prefix line plus a short option list, ``send()`` appends a follow-up embed with
  clickable buttons (:class:`AutoChoiceView`). A click injects the option back into the gateway as a
  fresh user turn — no ``clarify`` tool involved. See ``README.md`` for the trigger format.
* **Whitelist decision card.** ``send_whitelist_decision`` posts an Approve/Ignore/Skip embed
  (:class:`WhitelistDecisionView`) that drives the LINE override plugin's ``WhitelistStore``; the LINE
  plugin's ``notify_unauthorized`` picks it when the notify target is ``discord:<channel>``.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional

from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent, MessageType
from plugins.platforms.discord import adapter as _base

logger = logging.getLogger(__name__)


def _whitelist_store_class():
    """``WhitelistStore`` from the LINE override plugin: the module the plugin manager already loaded when
    the LINE platform is active in this process (any profile scope), else a path import from the installed
    plugin directory. Raises when neither is available."""
    for name, mod in list(sys.modules.items()):
        if name.startswith("hermes_plugins.platforms__line") and name.endswith(".whitelist_store") and mod is not None:
            cls = getattr(mod, "WhitelistStore", None)
            if cls is not None:
                return cls
    cached = sys.modules.get("hermes_fork_line_whitelist_store")
    if cached is not None:
        return cached.WhitelistStore
    from hermes_constants import get_hermes_home
    path = get_hermes_home() / "plugins" / "platforms" / "line" / "whitelist_store.py"
    spec = importlib.util.spec_from_file_location("hermes_fork_line_whitelist_store", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"LINE whitelist store not installed at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules["hermes_fork_line_whitelist_store"] = mod
    return mod.WhitelistStore


# ── Auto-detected choice buttons ──────────────────────────────────────────

# Circled-number markers (①..⑳) used by some models for option lists.
_CIRCLED_NUMBER_MARKERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# Ordered by strength of the "this is a menu" signal. The first style that yields >=2 options wins, so an
# explicit numbered menu is preferred over a loose bullet list that happens to share the message.
_CHOICE_LINE_PATTERNS = (
    re.compile(r"^\s*\d{1,2}\s*[.)、．]\s+(?P<text>\S.*)$"),
    re.compile(r"^\s*[" + _CIRCLED_NUMBER_MARKERS + r"]\s*(?P<text>\S.*)$"),
    re.compile(r"^\s*[A-Za-z]\s*[).]\s+(?P<text>\S.*)$"),
    re.compile(r"^\s*[-*•·]\s+(?P<text>\S.*)$"),
)

# Explicit format prefixes the agent uses to opt a reply into clickable buttons. Without one of these the
# reply is left alone, so incidental numbered/bulleted lists (steps, citations) never sprout buttons.
_SINGLE_CHOICE_PREFIX = "? "
_MULTI_CHOICE_PREFIX = "?? "


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _clean_choice_text(text: str) -> str:
    """Trim a parsed option down to a clean button label body: strip surrounding Markdown emphasis and
    trailing list punctuation."""
    opt = text.strip()
    opt = opt.strip("*_`").strip()
    opt = opt.rstrip(" ,，、;；")
    return opt.strip()


def _detect_choice_prefix(text: str) -> Optional[bool]:
    """``True`` for multi-select, ``False`` for single-select, ``None`` when no prefix line is present."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_MULTI_CHOICE_PREFIX):
            return True
        if stripped.startswith(_SINGLE_CHOICE_PREFIX):
            return False
    return None


def _extract_choice_question(content: str) -> str:
    """The question text off the prefix line, sans ``? ``/``?? `` marker (``""`` if none)."""
    if not content:
        return ""
    for line in content.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith(_MULTI_CHOICE_PREFIX):
            return stripped[len(_MULTI_CHOICE_PREFIX):].strip()
        if stripped.startswith(_SINGLE_CHOICE_PREFIX):
            return stripped[len(_SINGLE_CHOICE_PREFIX):].strip()
    return ""


def _detect_inline_choices(content: str, *, max_choices: int = 24) -> "tuple[List[str], bool]":
    """``(choices, multi_select)`` for an outbound message: clean option bodies WITHOUT their leading
    markers (the view adds its own numbering), capped at ``max_choices``. ``([], False)`` when no prefix
    line is present or fewer than two options parse out."""
    if not content or not content.strip():
        return [], False
    text = content.strip()

    mode = _detect_choice_prefix(text)
    if mode is None:
        return [], False
    multi_select = mode

    lines = text.splitlines()
    for pattern in _CHOICE_LINE_PATTERNS:
        items: List[str] = []
        for line in lines:
            m = pattern.match(line)
            if m:
                opt = _clean_choice_text(m.group("text"))
                if opt:
                    items.append(opt)
        items = _dedupe_keep_order(items)
        if len(items) >= 2:
            return items[:max_choices], multi_select

    return [], multi_select


def _fit_button_label(prefix: str, choice: str, *, limit: int = 80) -> str:
    """Discord button label (≤80 chars) cut at a word boundary: prefer the last space in the trailing half
    of the budget, then a soft boundary (``- , . )``), else a hard cut with an ellipsis."""
    budget = limit - len(prefix)
    if budget <= 1:
        return (prefix + choice)[:limit]
    if len(choice) <= budget:
        return f"{prefix}{choice}"
    truncated = choice[: budget - 1].rstrip()
    cut_at = -1
    space = truncated.rfind(" ")
    if space >= budget // 2:
        cut_at = space
    if cut_at < 0:
        latest_soft = max((truncated.rfind(s) for s in ("-", ",", ".", ")")), default=-1)
        if latest_soft >= budget // 2:
            cut_at = latest_soft + 1
    if cut_at > 0:
        truncated = truncated[:cut_at]
    return f"{prefix}{truncated.rstrip()}…"


class DiscordAdapter(_base.DiscordAdapter):
    """Upstream's adapter plus the auto-choice follow-up and the whitelist decision card."""

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        result = await super().send(chat_id, content, reply_to, metadata)
        # Nonconversational/system messages (history backfill, status echoes) aren't questions to the user.
        if getattr(result, "success", False) and not _base._metadata_marks_nonconversational(metadata):
            await self._maybe_send_choice_buttons(chat_id, content, reply_to, metadata)
        return result

    def _discord_auto_choice_buttons(self) -> bool:
        """Whether auto-detected choice buttons are enabled (default on). Disable via
        ``discord.auto_choice_buttons: false`` or ``DISCORD_AUTO_CHOICE_BUTTONS=false``."""
        configured = self.config.extra.get("auto_choice_buttons")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() not in {"false", "0", "no", "off"}
            return bool(configured)
        return os.getenv("DISCORD_AUTO_CHOICE_BUTTONS", "true").lower() not in {"false", "0", "no", "off"}

    async def _maybe_send_choice_buttons(
        self, chat_id: str, content: str, reply_to: Optional[str], metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Append clickable choice buttons when ``content`` poses a choice (see module docstring).

        Best-effort: any failure is swallowed (logged at debug) so a parsing or send hiccup never breaks
        the primary message delivery."""
        if not self._discord_auto_choice_buttons():
            return
        if not self._client or not _base.DISCORD_AVAILABLE:
            return
        try:
            choices, multi_select = _detect_inline_choices(content)
            if len(choices) < 2:
                return
            _ensure_views()

            # Resolve the same target channel send() used (thread wins).
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))
            if not channel or self._is_forum_parent(channel):
                return  # forum parents reject channel.send(); upstream posted the reply as a thread

            view = AutoChoiceView(
                adapter=self, choices=choices, allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids, multi_select=multi_select)
            question = _extract_choice_question(content)
            if multi_select:
                title = "❓ Hermes asks (multi-select)"
                hint = "Select any number, then ✅ Confirm — or ✏️ Other to type your own."
            else:
                title = "❓ Hermes asks"
                hint = "Tap a choice below, or ✏️ Other to type your own."
            description = f"{question}\n\n{hint}" if question else hint
            discord = _base.discord
            embed = discord.Embed(title=title, description=description, color=discord.Color.blue())
            msg = await channel.send(embed=embed, view=view)
            view._message = msg
        except Exception as e:
            logger.debug("[%s] auto-choice buttons skipped: %s", self.name, e)

    async def _inject_user_choice(self, interaction: Any, choice_text: str) -> None:
        """Feed a clicked choice back into the gateway as a fresh user turn: a :class:`MessageEvent` built
        from the interaction's channel + the clicking user (same session key a real message in that
        channel/thread would produce), dispatched via ``handle_message``. ``raw_message`` is ``None``."""
        channel = getattr(interaction, "channel", None)
        user = getattr(interaction, "user", None)
        if channel is None or user is None:
            return
        discord = _base.discord

        is_thread = isinstance(channel, discord.Thread)
        is_dm = isinstance(channel, discord.DMChannel)
        thread_id = str(channel.id) if is_thread else None
        parent_channel_id = self._get_parent_channel_id(channel) if is_thread else None
        guild = getattr(channel, "guild", None)

        if is_dm:
            chat_type = "dm"
            chat_name = getattr(user, "display_name", None) or getattr(user, "name", "DM")
        elif is_thread:
            chat_type = "thread"
            chat_name = self._format_thread_chat_name(channel)
        else:
            chat_type = "group"
            chat_name = getattr(channel, "name", str(channel.id))
            if guild is not None:
                chat_name = f"{getattr(guild, 'name', '')} / #{chat_name}"

        chat_topic = self._get_effective_topic(channel, is_thread=is_thread)

        source = self.build_source(
            chat_id=str(channel.id), chat_name=chat_name, chat_type=chat_type,
            user_id=str(getattr(user, "id", "")),
            user_name=getattr(user, "display_name", None) or getattr(user, "name", "user"),
            thread_id=thread_id, chat_topic=chat_topic, is_bot=bool(getattr(user, "bot", False)),
            guild_id=str(guild.id) if guild is not None else None, parent_chat_id=parent_channel_id,
            # The click already passed the component auth check; mirror on_message's role-authorized flag so
            # downstream gating treats this like a genuine allowlisted user message.
            role_authorized=bool(self._allowed_role_ids))

        event = MessageEvent(text=choice_text, message_type=MessageType.TEXT, source=source, raw_message=None)
        logger.info(
            "[%s] Auto-choice injected as user turn (chat=%s, user=%s, choice=%r)",
            self.name, source.chat_id, getattr(user, "display_name", "?"), choice_text)
        await self.handle_message(event)

    async def send_whitelist_decision(
        self, chat_id: str, source_type: str, source_id: str, name: str = "", metadata: Optional[dict] = None,
    ) -> SendResult:
        """Interactive LINE whitelist-decision card (Approve/Ignore/Skip), used by the LINE override's
        ``notify_unauthorized`` when the notify target is Discord."""
        if not self._client or not _base.DISCORD_AVAILABLE:
            return SendResult(success=False, error="Not connected")
        try:
            _ensure_views()
            target_id = chat_id
            if metadata and metadata.get("thread_id"):
                target_id = metadata["thread_id"]
            channel = self._client.get_channel(int(target_id))
            if not channel:
                channel = await self._client.fetch_channel(int(target_id))

            discord = _base.discord
            who = name or source_id
            embed = discord.Embed(title="🔔 LINE: unauthorized access attempt", color=discord.Color.orange())
            embed.add_field(name="type", value=str(source_type), inline=True)
            embed.add_field(name="name", value=str(who), inline=True)
            embed.add_field(name="id", value=f"`{source_id}`", inline=False)

            view = WhitelistDecisionView(
                source_type=source_type, source_id=source_id, allowed_user_ids=self._allowed_user_ids,
                allowed_role_ids=self._allowed_role_ids)
            msg = await channel.send(embed=embed, view=view)
            view._message = msg  # for on_timeout expiration editing
            return SendResult(success=True, message_id=str(msg.id))
        except Exception as e:
            return SendResult(success=False, error=str(e))


# View classes need the discord SDK; like upstream's ``_define_discord_view_classes`` they are defined once
# the SDK is importable (at import, or after a lazy install on first use).
AutoChoiceView = None
WhitelistDecisionView = None


def _ensure_views() -> None:
    if AutoChoiceView is None and _base.DISCORD_AVAILABLE:
        _define_fork_view_classes()


def _define_fork_view_classes() -> None:
    global AutoChoiceView, WhitelistDecisionView
    discord = _base.discord

    class AutoChoiceView(discord.ui.View):
        """Buttons auto-attached to replies that already pose a choice.

        No gateway clarify entry sits behind it: picking a button *injects* the chosen option back into
        the gateway as a fresh user message (``adapter._inject_user_choice``), exactly as if the user had
        typed it. ``✏️ Other`` dismisses the buttons so the user can type a free-form reply. Only
        allowlisted users/roles may click. Single-select is single-use; multi-select toggles selections
        (green = picked) and stays live until ``✅ Confirm`` injects the joined answer.
        """

        def __init__(self, adapter, choices: List[str], allowed_user_ids: set,
                     allowed_role_ids: Optional[set] = None, multi_select: bool = False):
            super().__init__(timeout=600)  # 10-minute window
            self._adapter = adapter
            self.choices = list(choices)[:24]
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False
            self._message = None
            self.multi_select = multi_select
            self._selected: "set[int]" = set()
            self._choice_buttons: List[Any] = []
            self._confirm_btn = None

            for index, choice in enumerate(self.choices):
                button = discord.ui.Button(
                    label=_fit_button_label(f"{index + 1}. ", choice), style=discord.ButtonStyle.primary,
                    custom_id=f"autochoice:{index}")
                button.callback = self._make_choice_callback(index, choice)
                self.add_item(button)
                self._choice_buttons.append(button)

            if self.multi_select:
                confirm_btn = discord.ui.Button(
                    label=self._confirm_label(), style=discord.ButtonStyle.success, custom_id="autochoice:confirm")
                confirm_btn.callback = self._on_confirm
                self.add_item(confirm_btn)
                self._confirm_btn = confirm_btn

            other_btn = discord.ui.Button(
                label="✏️ Other (type answer)", style=discord.ButtonStyle.secondary, custom_id="autochoice:other")
            other_btn.callback = self._on_other
            self.add_item(other_btn)

        def _confirm_label(self) -> str:
            return f"✅ Confirm ({len(self._selected)} selected)"

        def _check_auth(self, interaction) -> bool:
            return _base._component_check_auth(interaction, self.allowed_user_ids, self.allowed_role_ids)

        def _make_choice_callback(self, index: int, choice: str):
            async def _callback(interaction):
                await self._resolve_choice(interaction, index, choice)
            return _callback

        async def _disable_and_ack(self, interaction) -> None:
            self.resolved = True
            for child in self.children:
                child.disabled = True
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

        async def _resolve_choice(self, interaction, index: int, choice: str) -> None:
            if self.resolved:
                await interaction.response.send_message("This prompt has already been answered~", ephemeral=True)
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message("You're not authorized to answer this prompt~", ephemeral=True)
                return
            if self.multi_select:
                await self._toggle_choice(interaction, index)  # nothing is injected until ✅ Confirm
                return
            await self._disable_and_ack(interaction)
            try:
                await self._adapter._inject_user_choice(interaction, choice)
            except Exception:
                logger.error("Discord auto-choice injection failed", exc_info=True)

        async def _toggle_choice(self, interaction, index: int) -> None:
            """Flip option ``index`` on/off and repaint the buttons."""
            if index in self._selected:
                self._selected.discard(index)
            else:
                self._selected.add(index)
            for i, button in enumerate(self._choice_buttons):
                button.style = discord.ButtonStyle.success if i in self._selected else discord.ButtonStyle.primary
            if self._confirm_btn is not None:
                self._confirm_btn.label = self._confirm_label()
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                try:
                    await interaction.response.defer()
                except Exception:
                    pass

        async def _on_confirm(self, interaction) -> None:
            if self.resolved:
                await interaction.response.send_message("This prompt has already been answered~", ephemeral=True)
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message("You're not authorized to answer this prompt~", ephemeral=True)
                return
            if not self._selected:
                await interaction.response.send_message("Pick at least one option first~", ephemeral=True)
                return
            # Join the picked options into one user turn, in display order, with the ideographic comma.
            picked = [self.choices[i] for i in sorted(self._selected) if i < len(self.choices)]
            answer = "、".join(picked)
            await self._disable_and_ack(interaction)
            try:
                await self._adapter._inject_user_choice(interaction, answer)
            except Exception:
                logger.error("Discord auto-choice injection failed", exc_info=True)

        async def _on_other(self, interaction) -> None:
            if self.resolved:
                await interaction.response.send_message("This prompt has already been answered~", ephemeral=True)
                return
            if not self._check_auth(interaction):
                await interaction.response.send_message("You're not authorized to answer this prompt~", ephemeral=True)
                return
            await self._disable_and_ack(interaction)
            # The user's next normal message is picked up by on_message as usual. Just nudge them.
            try:
                await interaction.followup.send("Go ahead and type your answer~", ephemeral=True)
            except Exception:
                pass

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True
            msg = getattr(self, "_message", None)
            if msg:
                try:
                    await msg.edit(view=self)
                except Exception:
                    pass

    class WhitelistDecisionView(discord.ui.View):
        """Interactive LINE whitelist-decision card (Approve/Ignore/Skip).

        Tapping a button hits the LINE override plugin's ``WhitelistStore`` (approve/ignore) or is a no-op
        (skip). Auth mirrors ``ExecApprovalView`` (the adapter's user/role allowlist) plus a whitelist-admin
        gate on mutations. Every store touch is guarded: with the LINE plugin absent it degrades to an
        ephemeral error rather than crashing the interaction handler.
        """

        def __init__(self, source_type: str, source_id: str, allowed_user_ids: set,
                     allowed_role_ids: Optional[set] = None):
            super().__init__(timeout=86400)  # 24h — admin may not be online
            self.source_type = source_type
            self.source_id = source_id
            self.allowed_user_ids = allowed_user_ids
            self.allowed_role_ids = allowed_role_ids or set()
            self.resolved = False

        def _check_auth(self, interaction) -> bool:
            return _base._component_check_auth(interaction, self.allowed_user_ids, self.allowed_role_ids)

        @staticmethod
        def _load_store():
            """Best-effort WhitelistStore; None if the LINE override plugin is absent."""
            try:
                return _whitelist_store_class()()
            except Exception:
                return None

        async def _finish(self, interaction, color, footer: str):
            """Recolor the embed, disable buttons, and edit the message."""
            self.resolved = True
            embed = interaction.message.embeds[0] if interaction.message.embeds else None
            if embed is not None:
                embed.color = color
                embed.set_footer(text=footer)
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)

        async def _guard(self, interaction) -> bool:
            """Common pre-checks; True if the action may proceed."""
            if self.resolved:
                await interaction.response.send_message("This request has already been resolved~", ephemeral=True)
                return False
            if not self._check_auth(interaction):
                await interaction.response.send_message("You're not authorized~", ephemeral=True)
                return False
            return True

        async def _admin_ok(self, interaction, store) -> bool:
            """Whitelist-admin gate for mutating actions: a LINE whitelist admin OR the Discord recipient of
            the notify card (whose Discord id won't match the LINE admin list)."""
            try:
                if store is not None:
                    caller = str(interaction.user.id)
                    checker = getattr(store, "is_card_admin", None)
                    if checker("discord", caller) if checker else store.is_admin(caller):
                        return True
            except Exception:
                pass
            await interaction.response.send_message("You're not a whitelist admin~", ephemeral=True)
            return False

        @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.green)
        async def approve(self, interaction, button):
            if not await self._guard(interaction):
                return
            store = self._load_store()
            if store is None:
                await interaction.response.send_message("Whitelist unavailable~", ephemeral=True)
                return
            if not await self._admin_ok(interaction, store):
                return
            try:
                res = store.approve_pending(self.source_id, added_by=str(interaction.user.id))
            except Exception as exc:
                logger.error("Discord whitelist approve failed: %s", exc)
                await interaction.response.send_message("Approve failed~", ephemeral=True)
                return
            if isinstance(res, dict) and res.get("approved"):
                scope = res.get("scope")
                footer = f"✅ Approved{f' ({scope})' if scope else ''} by {interaction.user.display_name}"
                await self._finish(interaction, discord.Color.green(), footer)
            else:
                reason = res.get("reason") if isinstance(res, dict) else ""
                await interaction.response.send_message(
                    f"Not approved{f': {reason}' if reason else ''}~", ephemeral=True)

        @discord.ui.button(label="⛔ Ignore", style=discord.ButtonStyle.red)
        async def ignore(self, interaction, button):
            if not await self._guard(interaction):
                return
            store = self._load_store()
            if store is None:
                await interaction.response.send_message("Whitelist unavailable~", ephemeral=True)
                return
            if not await self._admin_ok(interaction, store):
                return
            try:
                ok = store.ignore_pending(self.source_id)
            except Exception as exc:
                logger.error("Discord whitelist ignore failed: %s", exc)
                await interaction.response.send_message("Ignore failed~", ephemeral=True)
                return
            if ok:
                await self._finish(interaction, discord.Color.greyple(), f"⛔ Ignored by {interaction.user.display_name}")
            else:
                await interaction.response.send_message("Nothing to ignore~", ephemeral=True)

        @discord.ui.button(label="➖ Skip", style=discord.ButtonStyle.grey)
        async def skip(self, interaction, button):
            if not await self._guard(interaction):
                return
            await self._finish(interaction, discord.Color.greyple(), f"➖ Skipped by {interaction.user.display_name}")

        async def on_timeout(self):
            self.resolved = True
            for child in self.children:
                child.disabled = True
            msg = getattr(self, "_message", None)
            if msg:
                try:
                    embed = msg.embeds[0] if msg.embeds else None
                    if embed is not None:
                        embed.color = discord.Color.greyple()
                        embed.set_footer(text="⏱ Expired — no action taken")
                    await msg.edit(embed=embed, view=self)
                except Exception:
                    pass

    globals()["AutoChoiceView"] = AutoChoiceView
    globals()["WhitelistDecisionView"] = WhitelistDecisionView


if _base.DISCORD_AVAILABLE:
    _define_fork_view_classes()


class _OverrideContext:
    """Delegate every registration to the real ``PluginContext``; only ``register_platform`` is
    intercepted, to swap the adapter factory, so upstream's kwargs are reused verbatim."""

    def __init__(self, ctx, **overrides: Any) -> None:
        self._ctx = ctx
        self._overrides = overrides

    def register_platform(self, *args: Any, **kwargs: Any):
        kwargs.update(self._overrides)
        return self._ctx.register_platform(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)


def register(ctx) -> None:
    """Plugin entry point: upstream's registration with this adapter."""
    _base.register(_OverrideContext(ctx, adapter_factory=DiscordAdapter))
