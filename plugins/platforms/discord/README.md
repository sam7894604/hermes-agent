# Discord Platform Adapter

Discord adapter for Hermes, supporting rich interactions including voice channels,
slash commands, and auto-choice buttons.

## Auto-Choice Buttons

When an agent reply contains a question followed by a short list of options,
the adapter automatically appends clickable Discord buttons — no `clarify` tool
needed. Clicking a button injects that option as a new user message, triggering
a fresh agent turn.

### Trigger Format

The agent opts a reply into buttons by starting a line with an explicit prefix:

| Prefix | Mode | Behaviour |
|--------|------|-----------|
| `? `   | Single-select | Clicking a choice button injects it immediately |
| `?? `  | Multi-select  | Buttons toggle (green = selected); a **✅ Confirm** button injects all picks joined with `、` |
| *(none)* | Off | No buttons — incidental lists, steps, or citations are left alone |

An **✏️ Other** button is always appended so the user can type a free-form reply.

### Example Agent Output

```
? Which plan do you want to activate?

1. Free
2. Pro
3. Enterprise
```

```
?? Which integrations do you need?

- Slack
- GitHub
- Linear
- Jira
```

### Rules & Limits

- At least **2 options** must parse out or no buttons are rendered.
- Maximum **24 choices** (Discord's button-per-message limit is 25; one slot
  is reserved for ✏️ Other).
- Supported list styles: `1.` / `1)` numbered, circled numbers `①②…`, letter
  options `A.` / `A)`, and bullet points `-` / `*` / `•`.
- The prefix line itself is stripped from the embed question text.

### Configuration

Enabled by default. To disable:

```yaml
# hermes.yaml  (under the discord plugin block)
discord:
  auto_choice_buttons: false
```

Or set the environment variable:

```
DISCORD_AUTO_CHOICE_BUTTONS=false
```

## Telegram

The same prefix convention (`? ` / `?? `) is planned for the Telegram adapter
and will share the detection logic via `auto_choice_utils.py`.
