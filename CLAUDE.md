# CLAUDE.md

Working notes for **Claude Code** when editing this repo. This is a companion to
[AGENTS.md](AGENTS.md) — AGENTS.md explains the codebase, this file explains how to
*work* on it effectively.

---

## Who you're working for

- Solo developer, Discord user **龍龍喵** (MASTER_ID `404111257008865280`).
- Chinese-speaking (Traditional). Keep commit messages / PR descriptions / long-form
  explanations in 繁體中文 when appropriate; short status updates can be English.
- Prefers **direct, terse responses**. No "Great idea!" preambles. No trailing summary of
  what you just did — the diff already shows it.

---

## Golden rules for this repo

Read [AGENTS.md § 3](AGENTS.md#3-hot-path-invariants--do-not-violate) first. Key points:

1. **Event loop is sacred.** Never put a `requests.get` / blocking file write in an
   async handler. Always `aiohttp` or `asyncio.to_thread`.
2. **All JSON state writes are atomic** (tmp + `os.replace`). Use
   [utils/json_store.py](utils/json_store.py) — don't roll your own.
3. **Save history through `save_history_async`** in hot path, never `save_history`.
4. **Pre-compile regex at module scope** for hot-path patterns.

If you're about to add a sync HTTP call or a plain `open(path, 'w')` inside an async
function, stop and check these invariants.

---

## Preferred working style

- **Reuse, don't re-invent.** Before writing a new helper, grep `utils/` and the
  module siblings — there is usually already a helper for member lookup, atomic JSON,
  text strip, AI callback wrapping, etc.
- **Delete, don't deprecate.** When removing code, delete it. Don't leave
  `# removed 2026-xx-xx` or keep unused branches around.
- **One PR = one intent.** If the user asks for a performance pass, don't also
  reformat unrelated files.
- **Short comments, non-obvious only.** Most code here documents itself via Chinese
  identifiers and docstrings at module top.

---

## When changing the AI chat flow

- The **queue → worker → save** pipeline in [gemini_worker.py](gemini_worker.py) is
  what keeps API-key rotation and rate-limiting correct. Do not skip the queue.
- Chat `rebuild` is expensive — only on `len(hist) >= HISTORY_MAX_TURNS`. Guard any
  new rebuild trigger behind a strict condition.
- When adding a new "context injector" (nicknames, knowledge, summary), inject it in
  the existing places (`_prepend_context` and `PERSONALITY`), not by shoving more
  system messages into every request.

---

## When adding a slash command

Minimal template:

```python
# commands/myfeature.py
import discord
from discord import app_commands
from utils.json_store import load_json, save_json_async

def setup(tree: app_commands.CommandTree) -> None:
    @tree.command(name="指令名", description="說明")
    async def _cmd(interaction: discord.Interaction, ...):
        await interaction.response.defer()
        ...
```

Then register in [commands/\_\_init\_\_.py](commands/__init__.py). That's the only
wire-up needed — `main.py` calls `commands.setup_all(tree)` at startup.

---

## Before declaring the task done

1. `python -m py_compile <files you changed>`
2. `python -c "import main"` — catches import-time regressions.
3. Re-read your diff once. Ask: "Did I change anything the user didn't ask for?"
4. If a behavioural change touches Discord UI, say plainly: *"I can't run Discord here;
   the code compiles but needs to be eyeballed in-server."*

---

## What NOT to do

- **Do not add `print` debug lines** and leave them committed. The repo uses
  [logger.py](logger.py) for structured output.
- **Do not change `PERSONALITY` strings** in [config.py](config.py) unless explicitly
  asked — that is the user-visible voice.
- **Do not regenerate `requirements.txt` from `pip freeze`**. It is hand-curated; a
  freeze pulls in transitive versions the user doesn't care to pin.
- **Do not move files around** for "cleanliness" without asking. Callers are spread
  across `commands/`, `main.py`, and `line_bot.py`.
- **Do not commit without being asked.** Always wait for explicit commit instruction.

---

## Useful file references

| Task | Start here |
|---|---|
| Add/adjust a slash command | [commands/](commands/) + [commands/\_\_init\_\_.py](commands/__init__.py) |
| Tweak chat pipeline / prompt | [gemini_worker.py](gemini_worker.py) + [config.py](config.py#L75-L92) |
| Add new JSON state | [utils/json_store.py](utils/json_store.py) |
| Reverse-image-search flow | [reverse_search.py](reverse_search.py) |
| Pixiv crawler | [pixiv_crawler/](pixiv_crawler/) + [pixiv_config.py](pixiv_config.py) |
| Graph rendering | [graph_render.py](graph_render.py) |
