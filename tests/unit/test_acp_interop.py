"""Opt-in live ACP interoperability smoke tests.

These tests consume real agent credentials and are skipped unless their
per-agent enable flag is set. The same ACPRuntime code path is used for every
agent; only external launch configuration differs.
"""

import json
import os
from pathlib import Path

import pytest

from agent_server.services.acp_launch import ACPLaunchConfig
from agent_server.services.acp_runtime import ACPRuntime


def _cases():
    return (
        ("hermes", "TRINITY_ACP_INTEROP_HERMES"),
        ("deepseek", "TRINITY_ACP_INTEROP_DEEPSEEK"),
    )


@pytest.mark.interop
@pytest.mark.asyncio
@pytest.mark.parametrize("agent,enable_variable", _cases())
async def test_live_acp_agent(agent, enable_variable, tmp_path):
    if os.environ.get(enable_variable) != "1":
        pytest.skip(f"set {enable_variable}=1 to run this credentialed smoke test")

    prefix = f"TRINITY_ACP_{agent.upper()}"
    command = os.environ.get(f"{prefix}_COMMAND")
    if not command:
        pytest.fail(f"{prefix}_COMMAND is required when the smoke test is enabled")
    try:
        args = json.loads(os.environ.get(f"{prefix}_ARGS", "[]"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{prefix}_ARGS must be a JSON string array: {exc}")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        pytest.fail(f"{prefix}_ARGS must be a JSON string array")

    runtime = ACPRuntime(
        ACPLaunchConfig(command, tuple(args)),
        env_provider=lambda: os.environ.copy(),
        cwd=Path(os.environ.get(f"{prefix}_CWD", tmp_path)),
    )
    try:
        response, _, metadata, session_id = await runtime.execute_headless(
            "Reply with exactly: Trinity ACP smoke OK",
            timeout_seconds=120,
        )
    finally:
        await runtime.close()

    assert response.strip(), "ACP agent returned no assistant text"
    assert session_id
    assert metadata.status == "success"
    assert runtime.get_capabilities().negotiated is True
