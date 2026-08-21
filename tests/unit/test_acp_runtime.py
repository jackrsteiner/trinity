"""Protocol and trust-contract tests for the generic ACP runtime."""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest


AGENT_SERVER = Path(__file__).parents[2] / "docker" / "base-image"
if str(AGENT_SERVER) not in sys.path:
    sys.path.insert(0, str(AGENT_SERVER))

from agent_server.services import acp_runtime  # noqa: E402
from agent_server.services.acp_runtime import ACPManifest, ACPRuntime  # noqa: E402


FAKE_SERVER = r'''#!/usr/bin/env python3
import json
import sys
import uuid

memory = ""

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    rid = request.get("id")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":1}})
    elif method == "session/new":
        send({"jsonrpc":"2.0","id":rid,"result":{"sessionId":str(uuid.uuid4())}})
    elif method == "session/prompt":
        text = request["params"]["prompt"][0]["text"]
        session_id = request["params"]["sessionId"]
        if text.startswith("remember "):
            memory = text.removeprefix("remember ")
            answer = "ACK"
        elif text == "recall":
            answer = memory
        elif text == "permission":
            send({"jsonrpc":"2.0","id":900,"method":"session/request_permission","params":{
                "sessionId":session_id,"options":[{"optionId":"once","kind":"allow_once","name":"Allow"}]
            }})
            permission = json.loads(sys.stdin.readline())
            answer = "allowed" if permission["result"]["outcome"].get("optionId") == "once" else "denied"
        elif text == "tool":
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call","toolCallId":"tool-1","title":"read_file","rawInput":{"path":"x"}
            }}})
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"tool-1","status":"completed","rawOutput":"ok"
            }}})
            answer = "tool-ok"
        else:
            answer = text
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
            "sessionUpdate":"agent_message_chunk","content":{"type":"text","text":answer}
        }}})
        send({"jsonrpc":"2.0","id":rid,"result":{"stopReason":"end_turn"}})
'''


@pytest.fixture
def fake_manifest(tmp_path, monkeypatch):
    launcher = tmp_path / "fake_acp.py"
    launcher.write_text(FAKE_SERVER)
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setattr(acp_runtime, "_read_only_enabled", lambda: False)

    def make(policy="reject"):
        return ACPManifest((sys.executable, str(launcher)), str(tmp_path), policy, True)

    return make


def test_runtime_declares_conservative_common_denominator():
    capabilities = ACPRuntime.capabilities()
    assert capabilities.chat_continuity is True
    assert capabilities.session_tab_resume is False
    assert capabilities.mcp_support is False
    assert capabilities.cost_reporting == "estimated"


def test_manifest_rejects_untrusted_owner(tmp_path):
    manifest = tmp_path / "runtime.json"
    manifest.write_text('{"version":1,"command":["/bin/false"]}')
    if os.getuid() == 0:
        pytest.skip("test requires an unprivileged test runner")
    with pytest.raises(RuntimeError, match="owned by root"):
        acp_runtime._load_manifest(manifest)


def test_persistent_chat_reuses_process_and_session(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest("reject"))

    async def scenario():
        first = await runtime.execute("remember MANGO-42", continue_session=True)
        process = runtime._chat.process
        second = await runtime.execute("recall", continue_session=True)
        assert first[0] == "ACK"
        assert second[0] == "MANGO-42"
        assert first[2].session_id == second[2].session_id
        assert runtime._chat.process is process
        runtime.reset_session()
        assert runtime._chat is None

    asyncio.run(scenario())


@pytest.mark.parametrize("policy,expected", [("allow", "allowed"), ("reject", "denied")])
def test_permission_reverse_rpc_obeys_manifest(fake_manifest, monkeypatch, policy, expected):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest(policy))

    async def scenario():
        response = await runtime.execute_headless("permission")
        assert response[0] == expected

    asyncio.run(scenario())


def test_tool_updates_translate_to_execution_log(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        response, log, metadata, session_id = await runtime.execute_headless("tool")
        assert response == "tool-ok"
        assert [entry.type for entry in log] == ["tool_use", "tool_result"]
        assert log[0].tool == "read_file"
        assert log[1].success is True
        assert metadata.tool_count == 1
        assert session_id

    asyncio.run(scenario())


def test_request_restrictions_fail_closed(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(RuntimeError, match="allowed_tools"):
            await runtime.execute_headless("x", allowed_tools=["Read"])
        with pytest.raises(RuntimeError, match="max_turns"):
            await runtime.execute_headless("x", max_turns=2)
        with pytest.raises(RuntimeError, match="persisted Session-tab resume"):
            await runtime.execute_headless("x", resume_session_id="old")

    asyncio.run(scenario())
