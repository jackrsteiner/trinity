import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from agent_server.services.acp_launch import ACPLaunchConfig
from agent_server.services.acp_runtime import ACPFeatureUnavailable, ACPRuntime


FIXTURE = Path(__file__).parents[1] / "fixtures" / "acp_conforming_agent.py"


def _runtime(tmp_path: Path, *, load: bool = False, allow_only: bool = False, permission_resolver=None):
    events = tmp_path / "events.jsonl"
    args = [str(FIXTURE), "--events", str(events)]
    if load:
        args.append("--load")
    if allow_only:
        args.append("--allow-only")
    runtime = ACPRuntime(
        ACPLaunchConfig(sys.executable, tuple(args)),
        env_provider=lambda: {},
        cwd=tmp_path,
        permission_resolver=permission_resolver,
    )
    return runtime, events


def _events(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_initialize_new_stream_and_clean_shutdown(tmp_path):
    runtime, events_path = _runtime(tmp_path)

    response, raw, metadata, session_id = await runtime.execute_headless("hello")

    assert response == "chunk-one chunk-two"
    assert metadata.input_tokens == 3
    assert metadata.output_tokens == 2
    assert metadata.cost_usd is None
    assert session_id == "new-1"
    assert runtime.get_capabilities().negotiated is True
    assert runtime.get_capabilities().session_load is False
    assert runtime.get_capabilities().prompt_images is True
    assert runtime.get_capabilities().cost_reporting == "unavailable"
    assert runtime.raw_capabilities["loadSession"] is False
    assert [item["update"]["sessionUpdate"] for item in raw] == [
        "agent_message_chunk",
        "agent_message_chunk",
    ]
    assert [item["event"] for item in _events(events_path)] == [
        "initialize",
        "new",
        "prompt",
        "close",
        "process_exit",
    ]


@pytest.mark.asyncio
async def test_load_only_when_advertised(tmp_path):
    unsupported, _ = _runtime(tmp_path / "unsupported")
    (tmp_path / "unsupported").mkdir()
    with pytest.raises(ACPFeatureUnavailable, match="session/load"):
        await unsupported.execute_headless("hello", resume_session_id="saved")

    supported_dir = tmp_path / "supported"
    supported_dir.mkdir()
    supported, events_path = _runtime(supported_dir, load=True)
    response, _, _, session_id = await supported.execute_headless(
        "hello", resume_session_id="saved"
    )
    assert response == "chunk-one chunk-two"
    assert session_id == "saved"
    assert supported.get_capabilities().session_load is True
    assert any(item == {"event": "load", "session_id": "saved", "mcp_count": 0} for item in _events(events_path))


@pytest.mark.asyncio
async def test_permission_default_deny_selects_reject_option(tmp_path):
    """No resolver ⇒ deny — expressed through the agent's own reject-kind
    option, not DeniedOutcome("cancelled"), which some agents read as a
    whole-turn cancellation."""
    runtime, events_path = _runtime(tmp_path)
    await runtime.execute_headless("permission")
    assert {
        "event": "permission",
        "outcome": "selected",
        "option_id": "deny",
    } in _events(events_path)


@pytest.mark.asyncio
async def test_permission_default_deny_cancels_without_reject_option(tmp_path):
    """An agent offering only allow-kind options gets the cancelled outcome —
    never an implicit allow."""
    runtime, events_path = _runtime(tmp_path, allow_only=True)
    await runtime.execute_headless("permission")
    assert {
        "event": "permission",
        "outcome": "cancelled",
        "option_id": None,
    } in _events(events_path)


@pytest.mark.asyncio
async def test_standard_stdio_mcp_configuration_is_forwarded(tmp_path):
    runtime, events_path = _runtime(tmp_path)
    assert runtime.configure_mcp(
        {
            "mcpServers": {
                "portable-server": {
                    "command": "example-mcp",
                    "args": ["--stdio"],
                    "env": {"EXAMPLE": "value"},
                }
            }
        }
    )
    await runtime.execute_headless("hello")
    new_event = next(item for item in _events(events_path) if item["event"] == "new")
    assert new_event["mcp_count"] == 1


@pytest.mark.asyncio
async def test_permission_round_trip_uses_resolver(tmp_path):
    runtime, events_path = _runtime(
        tmp_path,
        permission_resolver=lambda session_id, tool_call, options: "allow",
    )
    await runtime.execute_headless("permission")
    assert {
        "event": "permission",
        "outcome": "selected",
        "option_id": "allow",
    } in _events(events_path)


@pytest.mark.asyncio
async def test_cancellation_is_protocol_native(tmp_path):
    runtime, events_path = _runtime(tmp_path)
    task = asyncio.create_task(runtime.execute_headless("cancel", execution_id="exec-1"))
    for _ in range(100):
        if "exec-1" in runtime._active_prompts:
            break
        await asyncio.sleep(0.01)
    assert await runtime.cancel_execution("exec-1") is True
    _, _, metadata, _ = await asyncio.wait_for(task, timeout=2)
    assert metadata.status == "error"
    assert any(item["event"] == "cancel" for item in _events(events_path))


@pytest.mark.asyncio
async def test_protocol_errors_map_to_502_and_process_closes(tmp_path):
    """Agent/protocol failures surface as typed 502s (never bare 500s), with
    the original error text preserved in the sanitized detail."""
    runtime, events_path = _runtime(tmp_path)
    with pytest.raises(HTTPException, match="Internal error") as excinfo:
        await runtime.execute_headless("error")
    assert excinfo.value.status_code == 502
    assert "ACP agent error" in excinfo.value.detail
    assert [item["event"] for item in _events(events_path)][-2:] == ["close", "process_exit"]


@pytest.mark.asyncio
async def test_nonportable_controls_are_rejected(tmp_path):
    runtime, _ = _runtime(tmp_path)
    with pytest.raises(ACPFeatureUnavailable, match="model selection") as excinfo:
        await runtime.execute_headless("hello", model="agent-specific-model")
    # The refusal doubles as HTTP 409 so routers answer legibly, and the
    # backend never reads it as AUTH (503) or a rate signal (429).
    assert isinstance(excinfo.value, HTTPException)
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_read_only_mode_refuses_fail_closed(tmp_path, monkeypatch):
    """ACP has no portable read-only enforcement channel, so an enabled
    read-only flag REFUSES the turn instead of running unenforced. The
    unreadable/corrupt-config case stays fail-open (Codex loader parity)."""
    from agent_server.services import acp_runtime as acp_module

    config = tmp_path / "read-only-config.json"
    monkeypatch.setattr(acp_module, "_READ_ONLY_CONFIG", config)

    config.write_text('{"enabled": true}')
    runtime, _ = _runtime(tmp_path)
    with pytest.raises(ACPFeatureUnavailable, match="read-only") as excinfo:
        await runtime.execute_headless("hello")
    assert excinfo.value.status_code == 409
    with pytest.raises(ACPFeatureUnavailable, match="read-only"):
        await runtime.execute("hello")

    config.write_text("{not json")  # corrupt ⇒ fail-open, turn proceeds
    response, _, _, _ = await runtime.execute_headless("hello")
    assert response == "chunk-one chunk-two"


@pytest.mark.asyncio
async def test_chat_cost_is_per_turn_not_reaccumulated(tmp_path):
    """A cost reported in turn 1 must not leak into turn 2's metadata — the
    caller adds metadata.cost_usd to the session total every turn."""
    runtime, _ = _runtime(tmp_path)
    _, _, first, _ = await runtime.execute("cost", continue_session=True)
    assert first.cost_usd == 1.25
    _, _, second, _ = await runtime.execute("hello", continue_session=True)
    assert second.cost_usd is None
    await runtime.close()


@pytest.mark.asyncio
async def test_reset_chat_closes_persistent_session(tmp_path):
    """ADR 0002 §7: "New Chat" must close the live ACP process — otherwise the
    agent silently keeps the prior context across the reset."""
    runtime, events_path = _runtime(tmp_path)
    await runtime.execute("hello", continue_session=True)
    assert runtime._chat_session is not None
    await runtime.reset_chat()
    assert runtime._chat_session is None
    events = [item["event"] for item in _events(events_path)]
    assert "close" in events and "process_exit" in events
    # The next turn starts a fresh process/session rather than failing.
    response, _, _, _ = await runtime.execute("hello", continue_session=True)
    assert response == "chunk-one chunk-two"
    await runtime.close()
