import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_server.services.acp_launch import ACPLaunchConfig
from agent_server.services.acp_runtime import ACPFeatureUnavailable, ACPRuntime


FIXTURE = Path(__file__).parents[1] / "fixtures" / "acp_conforming_agent.py"


def _runtime(tmp_path: Path, *, load: bool = False, permission_resolver=None):
    events = tmp_path / "events.jsonl"
    args = [str(FIXTURE), "--events", str(events)]
    if load:
        args.append("--load")
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
async def test_permission_round_trip_defaults_to_denied(tmp_path):
    runtime, events_path = _runtime(tmp_path)
    await runtime.execute_headless("permission")
    assert {"event": "permission", "outcome": "cancelled"} in _events(events_path)


@pytest.mark.asyncio
async def test_permission_round_trip_uses_resolver(tmp_path):
    runtime, events_path = _runtime(
        tmp_path,
        permission_resolver=lambda session_id, tool_call, options: "allow",
    )
    await runtime.execute_headless("permission")
    assert {"event": "permission", "outcome": "selected"} in _events(events_path)


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
async def test_protocol_errors_propagate_and_process_closes(tmp_path):
    runtime, events_path = _runtime(tmp_path)
    with pytest.raises(Exception, match="Internal error"):
        await runtime.execute_headless("error")
    assert [item["event"] for item in _events(events_path)][-2:] == ["close", "process_exit"]


@pytest.mark.asyncio
async def test_nonportable_controls_are_rejected(tmp_path):
    runtime, _ = _runtime(tmp_path)
    with pytest.raises(ACPFeatureUnavailable, match="model selection"):
        await runtime.execute_headless("hello", model="agent-specific-model")
