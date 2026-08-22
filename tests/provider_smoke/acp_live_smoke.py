"""Provider-backed smoke tests for Trinity's generic ACP acceptance images.

This script runs inside a derived agent image. It deliberately prints only
phase-level results: provider credentials and model responses must never be
written to CI logs.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path


logging.basicConfig(level=logging.WARNING)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _configure_harness(harness: str) -> None:
    if harness == "hermes":
        _require(bool(os.getenv("GEMINI_API_KEY")), "GEMINI_API_KEY is unavailable")
        return

    _require(bool(os.getenv("DEEPSEEK_API_KEY")), "DEEPSEEK_API_KEY is unavailable")


async def _tool_round_trip(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime

    marker = f"TRINITY_{harness.upper()}_ACP_TOOL_OK"
    target = Path("/workspace/trinity-acp-tool-result.txt")
    target.unlink(missing_ok=True)
    runtime = ACPRuntime()
    response, execution_log, metadata, session_id = await runtime.execute_headless(
        (
            "Use an available filesystem or terminal tool to create the file "
            f"{target} containing exactly {marker}. Verify the file, then reply "
            f"with exactly {marker}. You must use a tool; do not merely describe it."
        ),
        timeout_seconds=300,
        execution_id=f"smoke-{harness}-tool",
    )
    _require(marker in response, "provider response omitted the tool marker")
    _require(target.is_file(), "the requested tool did not create the file")
    _require(target.read_text(encoding="utf-8").strip() == marker, "tool output was incorrect")
    tool_uses = [entry for entry in execution_log if entry.type == "tool_use"]
    tool_results = [entry for entry in execution_log if entry.type == "tool_result"]
    if harness == "hermes":
        _require(bool(tool_uses), "tool use was not translated")
        _require(any(entry.success for entry in tool_results), "successful tool result was not translated")
        _require(metadata.tool_count >= 1, "tool count metadata was not populated")
    else:
        # The pinned DeepSeek Harness ACP automation boundary intentionally
        # publishes committed assistant chunks only; tool trace stays in its
        # session log. If a future pin exposes tool events, require a complete
        # lifecycle instead of accepting a half-open tool card.
        _require(
            not tool_uses or any(entry.success for entry in tool_results),
            "DeepSeek exposed a tool start without a successful completion",
        )
        _require(metadata.tool_count == len(tool_uses), "tool count did not match exposed ACP events")
    _require(bool(session_id), "headless ACP session ID was empty")
    target.unlink(missing_ok=True)
    detail = "tool translation" if tool_uses else "provider-side tool execution"
    print(f"{harness}: live inference, stdout purity, and {detail} passed", flush=True)


async def _continuity_and_reset(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime

    token = f"TRINITY_{harness.upper()}_CONTINUITY_7319"
    runtime = ACPRuntime()
    first = await runtime.execute(
        f"Remember this exact token for the next turn: {token}. Reply with ACK.",
        continue_session=True,
        execution_id=f"smoke-{harness}-continuity-1",
    )
    second = await runtime.execute(
        "Reply with exactly the token I asked you to remember in the previous turn.",
        continue_session=True,
        execution_id=f"smoke-{harness}-continuity-2",
    )
    _require(token in second[0], "the continued ACP session lost conversational state")
    _require(first[2].session_id == second[2].session_id, "continued chat changed ACP sessions")
    old_session_id = second[2].session_id

    runtime.reset_session()
    after_reset = await runtime.execute(
        f"Reply with exactly TRINITY_{harness.upper()}_RESET_OK.",
        continue_session=True,
        execution_id=f"smoke-{harness}-reset",
    )
    _require(after_reset[2].session_id != old_session_id, "reset reused the previous ACP session")
    runtime.reset_session()
    print(f"{harness}: continuity and reset passed", flush=True)


async def _headless_isolation(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime

    runtime = ACPRuntime()

    async def run_one(suffix: str):
        marker = f"TRINITY_{harness.upper()}_ISOLATION_{suffix}"
        result = await runtime.execute_headless(
            f"Reply with exactly {marker} and nothing else.",
            timeout_seconds=300,
            execution_id=f"smoke-{harness}-isolation-{suffix.lower()}",
        )
        _require(marker in result[0], f"isolated response {suffix} was incorrect")
        return result[3]

    first_session, second_session = await asyncio.gather(run_one("ALPHA"), run_one("BETA"))
    _require(first_session != second_session, "parallel headless executions shared an ACP session")
    print(f"{harness}: parallel headless isolation passed", flush=True)


async def _read_only_contract(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime

    config = Path.home() / ".trinity" / "read-only-config.json"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.write_text('{"enabled":true}\n', encoding="utf-8")
    try:
        runtime = ACPRuntime()
        if harness == "hermes":
            try:
                await runtime.execute_headless("Reply with OK.", timeout_seconds=60)
            except Exception as exc:
                _require(
                    "cannot enforce Trinity read-only mode" in str(exc),
                    "Hermes read-only rejection returned the wrong failure",
                )
            else:
                raise AssertionError("Hermes widened unsupported read-only execution")
        else:
            blocked = Path("/workspace/trinity-acp-read-only-must-not-exist.txt")
            blocked.unlink(missing_ok=True)
            response, execution_log, _, _ = await runtime.execute_headless(
                (
                    "You must use a filesystem or terminal tool to create "
                    f"{blocked} containing BLOCKED. Attempt the tool now, then briefly report the result."
                ),
                timeout_seconds=300,
                execution_id="smoke-deepseek-read-only",
            )
            _require(bool(response.strip()), "DeepSeek returned no read-only result")
            _require(not blocked.exists(), "DeepSeek wrote a file while Trinity read-only mode was active")
            # DeepSeek's ACP transport deliberately omits internal tool trace.
            # A future transport revision may expose it; the filesystem remains
            # the authoritative enforcement assertion either way.
            _require(
                all(entry.type in {"tool_use", "tool_result"} for entry in execution_log),
                "DeepSeek emitted an unexpected execution-log entry",
            )
            runtime.reset_session()
    finally:
        config.unlink(missing_ok=True)
    print(f"{harness}: read-only contract passed", flush=True)


async def _cancellation(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime
    from agent_server.services.process_registry import get_process_registry

    execution_id = f"smoke-{harness}-cancel"
    runtime = ACPRuntime()
    registry = get_process_registry()
    task = asyncio.create_task(
        runtime.execute_headless(
            (
                "Use a terminal tool to sleep for 60 seconds, then reply with DONE. "
                "Start the tool immediately."
            ),
            timeout_seconds=180,
            execution_id=execution_id,
        )
    )
    for _ in range(200):
        if registry.get_status(execution_id):
            break
        if task.done():
            break
        await asyncio.sleep(0.05)
    _require(registry.get_status(execution_id) is not None, "execution finished before cancellation registration")
    confirmed = await asyncio.to_thread(runtime.cancel_execution, execution_id, 5.0)
    _require(confirmed, "ACP session/cancel was not acknowledged with stopReason=cancelled")
    try:
        await task
    except BaseException:
        pass
    else:
        raise AssertionError("cancelled ACP execution completed successfully")
    _require(registry.was_terminated(execution_id), "cancellation marker was not retained")
    print(f"{harness}: ACP protocol cancellation passed", flush=True)


async def _run(harness: str) -> None:
    from agent_server.services.acp_runtime import ACPRuntime

    _configure_harness(harness)
    _require(ACPRuntime().is_available(), "trusted ACP manifest or launcher is unavailable")
    print(f"{harness}: trusted manifest and launcher passed", flush=True)
    await _tool_round_trip(harness)
    await _continuity_and_reset(harness)
    await _headless_isolation(harness)
    await _read_only_contract(harness)
    await _cancellation(harness)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("harness", choices=("hermes", "deepseek"))
    args = parser.parse_args()
    asyncio.run(_run(args.harness))


if __name__ == "__main__":
    main()
