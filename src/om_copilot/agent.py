"""A bounded agent: Claude calls the MCP tools of this project in a loop (paid).

The tools are the ones a Claude Desktop user gets, called over the MCP protocol (in-process client to the same
server object). Limits: at most ``max_steps`` rounds of tool calls, then the model must give its final
structured reply; a spending cap in USD (estimated with the prices in baseline.py, an upper bound for
small prompts) stops the run. External calls stay behind the design-ingest guard.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from design_ingest.ask import DEFAULT_MODEL
from design_ingest.llm import guarded_create

from .baseline import cost_usd
from .mcp_server import Tools, build_server

log = logging.getLogger(__name__)

MAX_STEPS = 5
MAX_TOKENS = 1500


class BudgetStop(RuntimeError):
    """The estimated spend passed the cap."""


@dataclass
class Spend:
    cap_usd: float
    usd: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage: Any) -> None:
        self.usd += cost_usd(usage)
        self.calls += 1
        self.input_tokens += (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        if self.usd > self.cap_usd:
            raise BudgetStop(f"estimated spend ${self.usd:.3f} passed the cap ${self.cap_usd:.2f}")


@dataclass
class AgentRun:
    result: dict[str, Any] | None = None  # input of the final tool, None if the model never gave one
    calls: list[dict[str, Any]] = field(default_factory=list)  # name, arguments, output of every tool call
    rounds: int = 0
    forced: bool = False  # the step limit was reached and the final reply was forced
    seconds: float = 0.0
    usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def outputs(self) -> list[Any]:
        return [c["output"] for c in self.calls]


def _as_anthropic(tool: Any) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description or "", "input_schema": tool.input_schema}


async def run_agent(tools: Tools, *, client: Any, spend: Spend, system: str, user: str, allowed: set[str],
                    final_name: str, final_description: str, final_schema: dict[str, Any],
                    max_steps: int = MAX_STEPS, model: str | None = None,
                    allow_external: bool | None = None) -> AgentRun:
    from mcp import Client

    model = model or DEFAULT_MODEL
    run = AgentRun()
    started = time.perf_counter()
    before = (spend.usd, spend.input_tokens, spend.output_tokens)
    failure: Exception | None = None
    async with Client(build_server(tools)) as mcp:
        try:
            listed = (await mcp.list_tools()).tools
            specs = [_as_anthropic(t) for t in listed if t.name in allowed]
            final = {"name": final_name, "description": final_description, "input_schema": final_schema}
            messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
            for step in range(max_steps + 1):
                force = step == max_steps  # no more tool rounds: the next call must be the final reply
                choice = {"type": "tool", "name": final_name} if force else {"type": "auto"}
                resp = guarded_create(
                    client, purpose="agent", file="(mcp tools)", sha256="", kind="text",
                    amount=len(json.dumps(messages, default=str)), allow_external=allow_external, model=model,
                    max_tokens=MAX_TOKENS, system=system, tools=specs + [final], tool_choice=choice,
                    messages=messages)
                spend.add(resp.usage)
                messages.append({"role": "assistant", "content": resp.content})
                uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
                done = next((b for b in uses if b.name == final_name), None)
                if done is not None:
                    run.result, run.forced = dict(done.input), force
                    break
                if not uses:
                    break  # the model answered in plain text: no structured reply
                run.rounds += 1
                results = []
                for use in uses:
                    out = await mcp.call_tool(use.name, dict(use.input))
                    data = out.structured_content if out.structured_content is not None else [
                        getattr(c, "text", "") for c in out.content]
                    run.calls.append({"name": use.name, "arguments": dict(use.input), "output": data})
                    results.append({"type": "tool_result", "tool_use_id": use.id,
                                    "content": json.dumps(data, ensure_ascii=False), "is_error": bool(out.is_error)})
                messages.append({"role": "user", "content": results})
        except Exception as e:  # noqa: BLE001  the MCP client wraps errors in an ExceptionGroup: re-raise outside
            failure = e
    if failure is not None:
        raise failure
    run.seconds = time.perf_counter() - started
    run.usd = spend.usd - before[0]
    run.input_tokens, run.output_tokens = spend.input_tokens - before[1], spend.output_tokens - before[2]
    return run
