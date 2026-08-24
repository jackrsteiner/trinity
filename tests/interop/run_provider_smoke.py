#!/usr/bin/env python3
"""Credentialed ACP acceptance runner used inside derived provider images.

The image supplies only the external ACP launch vector and provider setup. This
runner always exercises Trinity's generic ACPRuntime and never imports either
harness. It intentionally fails when a requested acceptance behavior is not
observed; live provider checks are not allowed to silently skip.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from agent_server.services.acp_launch import load_acp_launch_config
from agent_server.services.acp_runtime import ACPFeatureUnavailable, ACPRuntime


def _require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def _expected_capabilities() -> dict[str, Any]:
    raw = os.environ.get("TRINITY_ACP_EXPECT_CAPABILITIES", "{}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("TRINITY_ACP_EXPECT_CAPABILITIES must be a JSON object")
    return value


def _reject_permission(_session_id: str, _tool_call: Any, options: list[Any]) -> str | None:
    """Select the agent's one-shot rejection option when it offers one."""
    for option in options:
        if option.kind in {"reject_once", "deny"}:
            return option.option_id
    return None


async def _wait_for_cancel(runtime: ACPRuntime, execution_id: str, task: asyncio.Task) -> bool:
    for _ in range(100):
        if task.done():
            return False
        if await runtime.cancel_execution(execution_id):
            return True
        await asyncio.sleep(0.05)
    return False


async def _run() -> None:
    agent = os.environ.get("TRINITY_ACP_INTEROP_AGENT", "unknown")
    workspace = Path(os.environ.get("TRINITY_ACP_WORKSPACE", "/workspace")).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = ACPRuntime(
        load_acp_launch_config(),
        env_provider=lambda: os.environ.copy(),
        cwd=workspace,
        permission_resolver=_reject_permission,
    )

    _require(runtime.is_available(), "configured ACP executable is unavailable")
    summary: dict[str, Any] | None = None
    try:
        response, raw_messages, metadata, session_id = await runtime.execute_headless(
            "Reply with exactly: Trinity ACP smoke OK",
            timeout_seconds=180,
        )
        _require("trinity acp smoke ok" in response.lower(), f"unexpected response: {response!r}")
        _require(bool(session_id), "session/new returned an empty session id")
        _require(metadata.status == "success", f"prompt status was {metadata.status!r}")
        _require(
            any(message.get("type") == "acp_session_update" for message in raw_messages),
            "prompt produced no streamed session/update messages",
        )

        capabilities = runtime.get_capabilities().to_dict()
        _require(capabilities["negotiated"] is True, "capabilities were not negotiated")
        for name, expected in _expected_capabilities().items():
            _require(name in capabilities, f"unknown expected capability: {name}")
            _require(
                capabilities[name] == expected,
                f"capability {name} was {capabilities[name]!r}, expected {expected!r}",
            )

        if capabilities["session_load"]:
            loaded_response, _, loaded_metadata, loaded_session_id = await runtime.execute_headless(
                "Reply with exactly: Trinity ACP load OK",
                resume_session_id=session_id,
                timeout_seconds=180,
            )
            _require(loaded_session_id == session_id, "session/load changed the session id")
            _require("trinity acp load ok" in loaded_response.lower(), "loaded prompt failed")
            _require(loaded_metadata.status == "success", "loaded prompt did not succeed")
        else:
            try:
                await runtime.execute_headless(
                    "This prompt must never run",
                    resume_session_id=session_id,
                    timeout_seconds=30,
                )
            except ACPFeatureUnavailable:
                pass
            else:
                raise AssertionError("session/load ran without an advertised capability")

        permission_response, permission_messages, _, _ = await runtime.execute_headless(
            "Use a filesystem or terminal tool to create /opt/trinity-acp-permission-probe. "
            "Attempt it exactly once; do not merely explain the command. Then report the result.",
            timeout_seconds=180,
        )
        permission_requests = [
            message
            for message in permission_messages
            if message.get("type") == "acp_permission_request"
        ]
        permission_responses = [
            message
            for message in permission_messages
            if message.get("type") == "acp_permission_response"
        ]
        _require(bool(permission_response.strip()), "permission probe returned no assistant text")
        _require(permission_requests, "agent did not issue an ACP permission request")
        _require(permission_responses, "Trinity did not return an ACP permission response")
        _require(
            not Path("/opt/trinity-acp-permission-probe").exists(),
            "denied permission probe unexpectedly created its target",
        )

        execution_id = f"interop-cancel-{uuid.uuid4()}"
        cancel_task = asyncio.create_task(
            runtime.execute_headless(
                "Produce a detailed, multi-section technical analysis of this workspace. "
                "Inspect files with tools before answering and continue until cancelled.",
                execution_id=execution_id,
                timeout_seconds=180,
            )
        )
        _require(
            await _wait_for_cancel(runtime, execution_id, cancel_task),
            "prompt completed before ACP cancellation could be delivered",
        )
        try:
            cancelled_result = await asyncio.wait_for(cancel_task, timeout=30)
        except Exception as exc:
            cancelled_result = None
            print(f"cancelled prompt settled with {type(exc).__name__}", file=sys.stderr)
        if cancelled_result is not None:
            _require(
                cancelled_result[2].status != "success",
                "cancelled prompt was reported as successful",
            )

        summary = {
            "agent": agent,
            "capabilities": capabilities,
            "raw_capabilities": runtime.raw_capabilities,
            "permission_requests": len(permission_requests),
            "cancellation": "delivered",
        }
    finally:
        await runtime.close()
    _require(summary is not None, "acceptance runner produced no summary")
    summary["shutdown"] = "clean"
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_run())
