"""Conversation loop: streams Claude's replies and executes tool calls through the Toolbox."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic

from .tools import Toolbox, tool_definitions

SYSTEM_PROMPT = """\
You manage the user's Kalshi prediction-market portfolio. You can read their balance, \
positions, orders, fills and market data, and you can place and cancel limit orders on \
their behalf.

Every order you submit is checked by a hard risk guard in code before it reaches Kalshi. \
If an order is rejected, the tool result says which limit it hit; do not try to get around \
a limit by splitting orders or switching to an equivalent position. Tell the user instead, \
and suggest what they could change if they want the trade. Call get_risk_limits when you \
need to know the limits or whether the session is in dry-run mode or halted.

How to work:
- Before any trade, look at the current portfolio and the market's order book. Base \
decisions on fresh tool data, not on numbers from earlier in the conversation.
- Kalshi prices are in cents (1-99) and a contract pays 100c if it resolves your way. A \
position of +N means N YES contracts; -N means N NO contracts. To exit, sell the side you \
hold.
- Use limit orders priced at or near the touch. When you exit, mind the spread and the \
available depth.
- Do the trades the user asks for, and use judgement on vague instructions such as "trim" \
or "tidy up" — state briefly what you chose and why. Do not add to losing positions or \
open new positions unless the user asked for it or clearly delegated that decision.
- If the user's request is ambiguous in a way that changes what you would trade (which \
market, how much), ask before trading.
- Give every order a one-sentence reason; it goes into the audit log.
- End each turn with a short summary of what you did: orders placed, rejected or \
cancelled, and the resulting positions. If a result was a dry run, say so.
- Money: report in dollars with cents (e.g. $12.34) in your replies.
"""

FALLBACK_STRIP_TYPES = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def sanitize_assistant_content(content: list[Any], *, drop_tool_use: bool = False) -> list[Any]:
    """Prepare response content for echoing back as history.

    After a server-side model fallback, model-internal blocks produced before the final
    ``fallback`` block must not be echoed. When a turn is truncated at max_tokens we also
    drop tool_use blocks, since we won't run them (and an unanswered tool_use is invalid).
    """
    last_fb = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    out = []
    for i, block in enumerate(content):
        if i < last_fb and block.type in FALLBACK_STRIP_TYPES:
            continue
        if drop_tool_use and block.type == "tool_use":
            continue
        out.append(block)
    return out


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        toolbox: Toolbox,
        *,
        model: str,
        effort: str,
        on_text: Callable[[str], None],
        on_tool_call: Callable[[str, Any], None],
        on_notice: Callable[[str], None],
    ):
        self.client = client
        self.toolbox = toolbox
        self.model = model
        self.effort = effort
        self.on_text = on_text
        self.on_tool_call = on_tool_call
        self.on_notice = on_notice
        self.tools = tool_definitions()
        self.messages: list[dict[str, Any]] = []

    def send(self, user_text: str) -> None:
        """Run one user turn to completion. On any exception (including Ctrl-C) the
        partial exchange is rolled back so the history stays valid."""
        start = len(self.messages)
        try:
            self._run_turn(user_text)
        except BaseException:
            del self.messages[start:]
            raise

    def _run_turn(self, user_text: str) -> None:
        self.messages.append({"role": "user", "content": user_text})
        while True:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                system=SYSTEM_PROMPT,
                tools=self.tools,  # type: ignore[arg-type]
                messages=self.messages,  # type: ignore[arg-type]
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},  # type: ignore[arg-type]
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                for text in stream.text_stream:
                    self.on_text(text)
                message = stream.get_final_message()

            stop = message.stop_reason
            if stop == "refusal":
                kept = [b for b in sanitize_assistant_content(message.content,
                                                              drop_tool_use=True)
                        if b.type == "text"]
                self.messages.append({"role": "assistant", "content": kept or
                                      [{"type": "text", "text": "(response declined)"}]})
                self.on_notice("The model declined to continue this request.")
                return

            truncated = stop == "max_tokens"
            content = sanitize_assistant_content(message.content, drop_tool_use=truncated)
            if not content:
                content = [{"type": "text", "text": "(no output)"}]
            self.messages.append({"role": "assistant", "content": content})

            if truncated:
                self.on_notice("Response hit the output limit; pending tool calls were "
                               "not executed.")
                return
            if stop == "pause_turn":
                continue
            if stop != "tool_use":
                return

            results = []
            for block in content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                self.on_tool_call(block.name, block.input)
                text, is_error = self.toolbox.run(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": text, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
