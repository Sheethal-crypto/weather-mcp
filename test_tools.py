"""
Client-side smoke test for the weather MCP server.

Connects over HTTP to a running weather_mcp_server.py, lists the tools, and
exercises all three of them plus one deliberate failure. The failure case is the
point of the exercise: an unresolvable location must come back as a normal tool
result carrying status "error", not as a raised exception, because an agent can
read a status field but cannot read a stack trace.

Start the server first, then:
    python test_tools.py
"""

import asyncio
import json
import os
import sys
from typing import Any, Dict

from fastmcp import Client

# Defaults to the local server. Override with WEATHER_MCP_URL when the server is
# on another port, or to point at the deployed Databricks app.
SERVER_URL = os.environ.get("WEATHER_MCP_URL", "http://localhost:8000/mcp")

results: list[tuple[str, bool, str]] = []


def payload_of(result: Any) -> Dict[str, Any]:
    """Pull the tool's dict out of a CallToolResult, whichever field carries it."""
    if getattr(result, "data", None) is not None:
        return result.data
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return {}


def check(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  -> {'PASS' if passed else 'FAIL'}: {detail}")


def show(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


async def main() -> int:
    print(f"Connecting to {SERVER_URL}\n")

    async with Client(SERVER_URL) as client:
        print("=" * 70)
        print("list_tools")
        print("=" * 70)
        tools = await client.list_tools()
        for tool in tools:
            summary = (tool.description or "").strip().splitlines()[0]
            print(f"  {tool.name}: {summary}")
        names = {tool.name for tool in tools}
        expected = {"get_current_weather", "get_forecast", "get_day_recommendation"}
        check("list_tools", expected <= names, f"found {sorted(names)}")

        print()
        print("=" * 70)
        print("get_current_weather('Pullman, Washington')")
        print("=" * 70)
        payload = payload_of(await client.call_tool("get_current_weather", {"location": "Pullman, Washington"}))
        show(payload)
        check(
            "get_current_weather",
            payload.get("status") == "ok" and payload.get("temperature") is not None,
            f"status={payload.get('status')} temperature={payload.get('temperature')}"
            f"{payload.get('units', {}).get('temperature', '')}",
        )

        print()
        print("=" * 70)
        print("get_forecast('Chicago', days=3)")
        print("=" * 70)
        payload = payload_of(await client.call_tool("get_forecast", {"location": "Chicago", "days": 3}))
        show(payload)
        days = payload.get("days") or []
        check(
            "get_forecast",
            payload.get("status") == "ok" and len(days) == 3,
            f"status={payload.get('status')} days returned={len(days)}",
        )

        print()
        print("=" * 70)
        print("get_day_recommendation('Seattle', date='tomorrow')")
        print("=" * 70)
        payload = payload_of(
            await client.call_tool("get_day_recommendation", {"location": "Seattle", "date": "tomorrow"})
        )
        show(payload)
        check(
            "get_day_recommendation",
            payload.get("status") == "ok" and "umbrella" in payload and "jacket" in payload,
            f"status={payload.get('status')} umbrella={payload.get('umbrella', {}).get('recommendation')} "
            f"jacket={payload.get('jacket', {}).get('recommendation')}",
        )

        print()
        print("=" * 70)
        print("FAILURE CASE: get_current_weather('Zzzzqqq Nowhereville')")
        print("=" * 70)
        try:
            payload = payload_of(
                await client.call_tool("get_current_weather", {"location": "Zzzzqqq Nowhereville"})
            )
        except Exception as exc:
            show({"raised": f"{type(exc).__name__}: {exc}"})
            check("error path", False, "tool raised instead of returning status 'error'")
        else:
            show(payload)
            check(
                "error path",
                payload.get("status") == "error" and bool(payload.get("error")),
                f"status={payload.get('status')} error={payload.get('error')!r}",
            )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed, _ in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    failed = [name for name, passed, _ in results if not passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
