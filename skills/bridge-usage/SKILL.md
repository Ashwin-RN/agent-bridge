---
name: bridge-usage
description: Quota left on each subscription (the 5h and weekly windows that Codex, Antigravity and the Claude plan report), then what the bridge has run in a period. Use when the user invokes bridge-usage, or asks how much quota is left or what the bridge has spent.
---

# Bridge usage

The figures come from the bridge and are already worked out. Your part is to ask for the right period and show the answer unchanged.

1. Read the period from what the user wrote after the skill name:
   - `today`, `7d`, `30d`, `all` or `session`: pass it as `period`.
   - A date or a time, such as `since 2026-11-20` or `2026-11-20 to 2026-11-25`: pass `since`, and `until` if one is given, each as `YYYY-MM-DD` or an ISO time such as `2026-11-20T14:30`. Leave `period` out.
   - Nothing: pass no period. The bridge then shows today.
2. Call the `usage` tool of the MCP server named `agent-bridge` with `view="dashboard"` and only those period arguments. An older bridge has a `usage_dashboard` tool instead of `usage`: call that one with the period arguments alone.
3. Reply with the result's `text` field inside one code block, exactly as received, line for line. Do not add, round, total, convert, reorder or restate any figure, and add no table of your own.
4. After the code block, write nothing, unless the user asked a question along with the skill name. Then answer it in one or two sentences, using only figures that appear in the text.

If the tool returns an error, show the error text as it is. If the period was not understood, the error says what to give instead.

If `agent-bridge` has neither a `usage` tool nor a `usage_dashboard` tool, the bridge in this host is out of date or not connected. Say so, and that it needs updating and reconnecting. Do not build a view from any other tool.
