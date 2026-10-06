"""Claude client + the tool-use loop every agent here runs on.

A hand-written loop rather than the SDK's Tool Runner: each turn has to be
priced against the batch budget before the next one is sent, and every tool call
is recorded for the dashboard. The loop ends when the model calls the agent's
`submit_*` tool (its structured answer), runs out of turns, or hits the budget.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("agentic.claude")

# $ per million tokens: (input, output, cache write, cache read).
_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}

MAX_TURNS = 25


class BudgetExceeded(Exception):
    pass


class ClaudeUnavailable(Exception):
    pass


def configured() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def _client():
    if not configured():
        raise ClaudeUnavailable("ANTHROPIC_API_KEY is not set in .env")
    import anthropic
    return anthropic.Anthropic(max_retries=3, timeout=600.0)


def cost_of(model: str, usage: Any) -> float:
    pin, pout, pwrite, pread = _PRICES.get(model, _PRICES["claude-opus-5-5"])
    g = lambda k: (getattr(usage, k, 0) or 0)  # noqa: E731
    return (g("input_tokens") * pin + g("output_tokens") * pout
            + g("cache_creation_input_tokens") * pwrite
            + g("cache_read_input_tokens") * pread) / 1_000_000


@dataclass
class LoopResult:
    submitted: Optional[Dict[str, Any]] = None   # the submit tool's input, if called
    cost_usd: float = 0.0
    turns: int = 0
    stop: str = ""                                # submitted | no_submit | refusal | max_tokens | max_turns | budget | error
    error: str = ""
    tool_log: List[Dict[str, Any]] = field(default_factory=list)


def run_loop(*, system: str, user_content: Any, tools: List[Dict[str, Any]],
             handlers: Dict[str, Callable[[Dict[str, Any]], Any]], submit_tool: str,
             model: str, budget_left_usd: float, effort: str = "high",
             max_turns: int = MAX_TURNS) -> LoopResult:
    """Run one agent to completion. `handlers[name](input)` returns a string or a
    list of content blocks (text/image). Never raises for model/tool problems —
    the outcome is in `LoopResult.stop`."""
    import anthropic

    res = LoopResult()
    try:
        client = _client()
    except ClaudeUnavailable as e:
        res.stop, res.error = "error", str(e)
        return res

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_content}]
    nudged = False
    while res.turns < max_turns:
        if res.cost_usd >= budget_left_usd:
            res.stop = "budget"
            return res
        res.turns += 1
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
                # A safety classifier decline re-runs on Anthropic's recommended
                # fallback model instead of ending the diagnosis.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            res.stop, res.error = "error", "Anthropic API key was rejected"
            return res
        except anthropic.BadRequestError as e:
            msg = str(e.message)
            if "credit balance" in msg.lower():
                msg = ("The Anthropic account behind ANTHROPIC_API_KEY has no credits. Add credits at "
                       "console.anthropic.com → Plans & Billing, then try again.")
            else:
                msg = f"Bad request: {msg}"
            res.stop, res.error = "error", msg
            return res
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            res.stop, res.error = "error", f"Claude API unavailable: {e}"
            return res

        res.cost_usd += cost_of(model, resp.usage)

        if resp.stop_reason == "refusal":
            res.stop = "refusal"
            return res
        if resp.stop_reason == "max_tokens":
            res.stop = "max_tokens"
            return res

        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue

        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            if nudged:
                res.stop = "no_submit"
                return res
            nudged = True
            messages.append({"role": "user", "content":
                             f"Finish by calling {submit_tool} with your answer."})
            continue

        results = []
        for call in calls:
            if call.name == submit_tool:
                res.submitted = dict(call.input or {})
                res.stop = "submitted"
                res.tool_log.append({"tool": call.name, "input": res.submitted})
                return res
            fn = handlers.get(call.name)
            entry = {"tool": call.name, "input": call.input}
            try:
                if fn is None:
                    raise ValueError(f"unknown tool {call.name}")
                out = fn(dict(call.input or {}))
                content = out if isinstance(out, list) else str(out)[:60000]
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
                entry["ok"] = True
            except Exception as e:  # a tool failure is information for the model, not a crash
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": f"Error: {e}", "is_error": True})
                entry["ok"], entry["error"] = False, str(e)[:300]
            res.tool_log.append(entry)
        messages.append({"role": "user", "content": results})

    res.stop = "max_turns"
    return res


def strict_tool(name: str, description: str, properties: Dict[str, Any],
                required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False,
        },
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=1, default=str, sort_keys=True)
