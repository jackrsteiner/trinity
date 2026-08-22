"""Protocol and trust-contract tests for the generic ACP runtime."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

AGENT_SERVER = Path(__file__).parents[2] / "docker" / "base-image"
if str(AGENT_SERVER) not in sys.path:
    sys.path.insert(0, str(AGENT_SERVER))

from agent_server.services import acp_runtime  # noqa: E402
from agent_server.services.acp_runtime import ACPManifest, ACPRuntime  # noqa: E402
from agent_server import state as state_module  # noqa: E402
from agent_server.models import ModelRequest  # noqa: E402
from agent_server.routers import chat as chat_router  # noqa: E402

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import sys
import uuid

memory = ""
initialize_params = {}

def send(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    rid = request.get("id")
    if method == "initialize":
        initialize_params = request["params"]
        send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":1,"authMethods":[
            {"id":"provider-api-key","name":"Use injected provider key"}
        ]}})
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
                "sessionId":session_id,"options":[
                    {"optionId":"always","kind":"allow_always","name":"Always"},
                    {"optionId":"once","kind":"allow_once","name":"Once"}
                ]
            }})
            permission = json.loads(sys.stdin.readline())
            answer = "allowed" if permission["result"]["outcome"].get("optionId") == "once" else "denied"
        elif text == "tool":
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call","toolCallId":"tool-1","title":"read_file","status":"pending","rawInput":{"path":"x"}
            }}})
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"tool-1","status":"in_progress","content":{"type":"text","text":"working"}
            }}})
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"tool-1","content":{"type":"text","text":"still working"}
            }}})
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"tool-1","status":"completed","rawOutput":"ok"
            }}})
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"tool-1","status":"completed","rawOutput":"duplicate"
            }}})
            answer = "tool-ok"
        elif text == "capabilities":
            answer = json.dumps(initialize_params["clientCapabilities"], sort_keys=True)
        elif text == "cancelled":
            send({"jsonrpc":"2.0","id":rid,"result":{"stopReason":"cancelled"}})
            continue
        elif text == "bad-json":
            print("not-json", flush=True)
            continue
        elif text == "model":
            answer = os.environ.get("ACP_MODEL", "missing")
        else:
            answer = text
        send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
            "sessionUpdate":"agent_message_chunk","content":{"type":"text","text":answer}
        }}})
        send({"jsonrpc":"2.0","id":rid,"result":{"stopReason":"end_turn"}})
"""


@pytest.fixture
def fake_manifest(tmp_path, monkeypatch):
    launcher = tmp_path / "fake_acp.py"
    launcher.write_text(FAKE_SERVER)
    launcher.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    monkeypatch.setattr(acp_runtime, "_read_only_enabled", lambda: False)
    # The fixture is intentionally in pytest's user-owned temporary directory;
    # individual manifest tests exercise the production trust checks directly.
    monkeypatch.setattr(acp_runtime, "_trusted_file", lambda *_a, **_kw: None)

    def make(policy="reject"):
        return ACPManifest((sys.executable, str(launcher)), str(tmp_path), policy, True)

    return make


def test_runtime_declares_conservative_common_denominator():
    capabilities = ACPRuntime.capabilities()
    assert capabilities.chat_continuity is True
    assert capabilities.session_tab_resume is False
    assert capabilities.mcp_support is False
    assert capabilities.cost_reporting == "unavailable"


@pytest.mark.parametrize(
    "result,match",
    [
        ({"protocolVersion": 2}, "version mismatch"),
        ({"protocolVersion": True}, "version mismatch"),
        ({"protocolVersion": 1, "authMethods": {}}, "must be an array"),
        ({"protocolVersion": 1, "authMethods": ["login"]}, "must be an object"),
        ({"protocolVersion": 1, "authMethods": [{}]}, "id must be a string"),
        ({"protocolVersion": 1, "agentCapabilities": []}, "must be an object"),
    ],
)
def test_initialize_rejects_unsupported_negotiation(result, match):
    with pytest.raises(RuntimeError, match=match):
        acp_runtime._ACPConnection._validate_initialize_result(result)


def test_initialize_allows_advertised_login_when_environment_is_already_authenticated():
    acp_runtime._ACPConnection._validate_initialize_result(
        {
            "protocolVersion": 1,
            "authMethods": [{"id": "provider-api-key", "name": "Provider API key"}],
        }
    )


def test_manifest_rejects_untrusted_owner(tmp_path, monkeypatch):
    manifest = tmp_path / "runtime.json"
    manifest.write_text('{"version":1,"command":["/bin/false"]}')
    if os.getuid() == 0:
        pytest.skip("test requires an unprivileged test runner")
    monkeypatch.setattr(
        acp_runtime, "_validate_trusted_parent_chain", lambda _path: None
    )
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


@pytest.mark.parametrize(
    "policy,expected", [("allow", "allowed"), ("reject", "denied")]
)
def test_permission_reverse_rpc_obeys_manifest(
    fake_manifest, monkeypatch, policy, expected
):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest(policy))

    async def scenario():
        response = await runtime.execute_headless("permission")
        assert response[0] == expected

    asyncio.run(scenario())


def test_tool_updates_translate_only_terminal_state_to_execution_log(
    fake_manifest, monkeypatch
):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())
    completions = []
    monkeypatch.setattr(
        acp_runtime,
        "complete_tool_execution",
        lambda tool_id, success, output: completions.append((tool_id, success, output)),
    )

    async def scenario():
        response, log, metadata, session_id = await runtime.execute_headless("tool")
        assert response == "tool-ok"
        assert [entry.type for entry in log] == ["tool_use", "tool_result"]
        assert log[0].tool == "read_file"
        assert log[1].success is True
        assert log[1].output == "ok"
        assert completions == [("tool-1", True, "ok")]
        assert metadata.tool_count == 1
        assert session_id

    asyncio.run(scenario())


def test_request_restrictions_fail_closed(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(HTTPException, match="allowed_tools") as allowed:
            await runtime.execute_headless("x", allowed_tools=["Read"])
        assert allowed.value.status_code == 422
        with pytest.raises(HTTPException, match="max_turns") as turns:
            await runtime.execute_headless("x", max_turns=2)
        assert turns.value.status_code == 422
        with pytest.raises(
            HTTPException, match="persisted Session-tab resume"
        ) as resume:
            await runtime.execute_headless("x", resume_session_id="old")
        assert resume.value.status_code == 422

    asyncio.run(scenario())


def test_model_is_propagated_to_harness_and_metadata(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        response, _log, metadata, _session_id = await runtime.execute_headless(
            "model", model="provider-model-42"
        )
        assert response == "provider-model-42"
        assert metadata.model_name == "provider-model-42"
        assert metadata.context_window == 0

    asyncio.run(scenario())


def test_initialize_does_not_advertise_unimplemented_filesystem_rpc(
    fake_manifest, monkeypatch
):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        response, *_rest = await runtime.execute_headless("capabilities")
        capabilities = json.loads(response)
        assert capabilities["fs"] == {"readTextFile": False, "writeTextFile": False}
        assert capabilities["terminal"] is False

    asyncio.run(scenario())


def test_cancelled_stop_reason_is_not_reported_as_success(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(HTTPException) as cancelled:
            await runtime.execute_headless("cancelled")
        assert cancelled.value.status_code == 499

    asyncio.run(scenario())


def test_protocol_error_discards_persistent_connection(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(HTTPException, match="non-JSON"):
            await runtime.execute("bad-json", continue_session=True)
        assert runtime._chat is None
        response = await runtime.execute("healthy", continue_session=True)
        assert response[0] == "healthy"
        runtime.reset_session()

    asyncio.run(scenario())


def test_protocol_transcript_has_aggregate_limit(monkeypatch):
    monkeypatch.setattr(acp_runtime, "MAX_PROTOCOL_TRANSCRIPT_BYTES", 8)
    state = acp_runtime._PromptState()
    with pytest.raises(RuntimeError, match="transcript exceeded size"):
        acp_runtime._ACPConnection._record_raw_message({"value": "too large"}, state)


def test_active_prompt_sends_protocol_cancel_before_process_cleanup(monkeypatch):
    connection = acp_runtime._ACPConnection(ACPManifest(("/bin/false",)))
    connection.process = SimpleNamespace(poll=lambda: None)
    connection.session_id = "session-42"
    connection._prompt_active = True
    sent = []
    monkeypatch.setattr(connection, "_write", sent.append)

    connection.cancel_prompt()

    assert sent == [
        {
            "jsonrpc": "2.0",
            "method": "session/cancel",
            "params": {"sessionId": "session-42"},
        }
    ]


@pytest.mark.parametrize(
    "message,status",
    [
        ("429 too many requests", 429),
        ("invalid api key", 503),
        ("request timed out", 504),
        ("ACP server closed stdout", 502),
        ("harness does not support images", 422),
        ("unexpected provider failure", 500),
    ],
)
def test_error_mapping_contract(message, status):
    assert acp_runtime._map_acp_exception(RuntimeError(message)).status_code == status


def test_agent_health_checks_acp_manifest_instead_of_claude(monkeypatch):
    sentinel = object()
    fake = type("FakeRuntime", (), {"is_available": lambda self: sentinel})()
    # Several legacy tests deliberately reload the agent_server subtree. Patch
    # the live module identity used by state.py's local import, not the
    # collection-time reference captured above.
    live_acp_runtime = importlib.import_module("agent_server.services.acp_runtime")
    monkeypatch.setattr(live_acp_runtime, "get_acp_runtime", lambda: fake)
    state = state_module.AgentState.__new__(state_module.AgentState)
    state.agent_runtime = "acp"
    state._check_claude_code = lambda: (_ for _ in ()).throw(
        AssertionError("Claude health fallback")
    )

    assert state._check_runtime_available() is sentinel


def test_model_routes_report_update_and_reset_acp(monkeypatch):
    resets = []
    fake = type(
        "FakeRuntime",
        (),
        {
            "get_default_model": lambda self: "derived-default",
            "reset_session": lambda self: resets.append(True),
        },
    )()
    monkeypatch.setattr(chat_router, "get_runtime", lambda: fake)
    monkeypatch.setattr(chat_router.agent_state, "agent_runtime", "acp")
    monkeypatch.setattr(chat_router.agent_state, "current_model", None)

    async def scenario():
        current = await chat_router.get_model()
        assert current["model"] == "derived-default"
        changed = await chat_router.set_model(ModelRequest(model="provider-v2"))
        assert changed["model"] == "provider-v2"
        assert chat_router.agent_state.current_model == "provider-v2"
        assert resets == [True]

    asyncio.run(scenario())
