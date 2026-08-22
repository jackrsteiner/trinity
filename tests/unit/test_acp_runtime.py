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
import time
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
        elif text == "secret-tool":
            secret = "sk-livecredential012345678901234"
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call","toolCallId":"secret-tool","title":"run " + secret,
                "status":"pending","rawInput":{"command":"TOKEN=" + secret}
            }}})
            time.sleep(0.1)
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call_update","toolCallId":"secret-tool","status":"completed",
                "rawOutput":"Bearer " + secret
            }}})
            answer = "secret-tool-ok"
        elif text == "dangling-tool":
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"tool_call","toolCallId":"dangling","title":"sleep","status":"in_progress","rawInput":{}
            }}})
            answer = "dangling-tool-finished"
        elif text == "usage-1" or text == "usage-2":
            amount = 1.25 if text == "usage-1" else 2.0
            used = 1200 if text == "usage-1" else 1800
            send({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                "sessionUpdate":"usage_update","used":used,"size":32768,
                "cost":{"amount":amount,"currency":"USD"}
            }}})
            answer = text
        elif text == "batch":
            print(json.dumps([
                {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":session_id,"update":{
                    "sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"batch-ok"}
                }}},
                {"jsonrpc":"2.0","id":rid,"result":{"stopReason":"end_turn"}}
            ], separators=(",", ":")), flush=True)
            continue
        elif text == "capabilities":
            answer = json.dumps(initialize_params["clientCapabilities"], sort_keys=True)
        elif text in {"cancelled", "max_tokens", "max_turn_requests", "refusal", "unknown-stop"}:
            reason = "future_reason" if text == "unknown-stop" else text
            send({"jsonrpc":"2.0","id":rid,"result":{"stopReason":reason}})
            continue
        elif text == "missing-stop":
            send({"jsonrpc":"2.0","id":rid,"result":{}})
            continue
        elif text == "wait-for-cancel":
            cancellation = json.loads(sys.stdin.readline())
            if cancellation.get("method") != "session/cancel":
                raise RuntimeError("expected session/cancel")
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
    manifest_module = importlib.import_module("agent_server.acp_manifest")
    monkeypatch.setattr(manifest_module, "trusted_file", lambda *_a, **_kw: None)

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
        (
            {"protocolVersion": 1, "authMethods": [{"id": "login"}]},
            "name must be a string",
        ),
        (
            {
                "protocolVersion": 1,
                "authMethods": [
                    {"id": "login", "name": "Terminal login", "type": "terminal"}
                ],
            },
            "terminal authentication",
        ),
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
    # Older tests reload the agent_server subtree during the full suite. Patch
    # the exact loader function's module globals rather than whichever module
    # object currently occupies sys.modules.
    monkeypatch.setitem(
        acp_runtime._load_manifest.__globals__,
        "_validate_trusted_parent_chain",
        lambda _path: None,
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
        assert metadata.context_window == 200000

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
        assert capabilities["auth"] == {"terminal": False}

    asyncio.run(scenario())


def test_cancelled_stop_reason_is_not_reported_as_success(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(HTTPException) as cancelled:
            await runtime.execute_headless("cancelled")
        assert cancelled.value.status_code == 499

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "prompt,status,detail",
    [
        ("max_tokens", 502, "token limit"),
        ("max_turn_requests", 422, "agent request limit"),
        ("refusal", 422, "refused"),
        ("missing-stop", 500, "missing stopReason"),
        ("unknown-stop", 500, "unknown stopReason"),
    ],
)
def test_every_non_success_or_invalid_stop_reason_fails(
    fake_manifest, monkeypatch, prompt, status, detail
):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        with pytest.raises(HTTPException) as stopped:
            await runtime.execute_headless(prompt)
        assert stopped.value.status_code == status
        assert detail in str(stopped.value.detail)

    asyncio.run(scenario())


def test_json_rpc_batch_processes_notifications_before_return(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        response, *_rest = await runtime.execute_headless("batch")
        assert response == "batch-ok"

    asyncio.run(scenario())


def test_usage_update_populates_context_and_turn_cost(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())

    async def scenario():
        first = await runtime.execute("usage-1", continue_session=True)
        second = await runtime.execute("usage-2", continue_session=True)
        assert first[2].input_tokens == 1200
        assert first[2].context_window == 32768
        assert first[2].cost_usd == 1.25
        assert second[2].input_tokens == 1800
        assert second[2].context_window == 32768
        assert second[2].cost_usd == 0.75
        runtime.reset_session()

    asyncio.run(scenario())


def test_live_tool_stream_and_activity_are_sanitized(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())
    activity_starts = []
    activity_completions = []
    monkeypatch.setattr(
        acp_runtime,
        "start_tool_execution",
        lambda tool_id, name, value: activity_starts.append((tool_id, name, value)),
    )
    monkeypatch.setattr(
        acp_runtime,
        "complete_tool_execution",
        lambda tool_id, success, output: activity_completions.append(
            (tool_id, success, output)
        ),
    )
    registry = acp_runtime.get_process_registry()

    async def scenario():
        task = asyncio.create_task(
            runtime.execute_headless("secret-tool", execution_id="live-secret")
        )
        for _ in range(200):
            queue = registry.subscribe_logs("live-secret")
            if queue is not None:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("live stream was not registered")
        streamed = []
        while len(streamed) < 2:
            entry = await asyncio.wait_for(queue.get(), timeout=2)
            if entry.get("type") != "stream_end":
                streamed.append(entry)
        await task
        rendered = json.dumps(
            {"streamed": streamed, "starts": activity_starts, "ends": activity_completions}
        )
        assert "sk-livecredential" not in rendered
        assert "***REDACTED***" in rendered
        registry.unsubscribe_logs("live-secret", queue)

    asyncio.run(scenario())


def test_prompt_cleanup_closes_pending_tool_activity(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())
    completions = []
    monkeypatch.setattr(
        acp_runtime,
        "complete_tool_execution",
        lambda tool_id, success, output: completions.append((tool_id, success, output)),
    )

    async def scenario():
        response, log, *_rest = await runtime.execute_headless("dangling-tool")
        assert response == "dangling-tool-finished"
        assert [entry.type for entry in log] == ["tool_use", "tool_result"]
        assert log[-1].success is False
        assert completions == [
            ("dangling", False, "ACP prompt ended before the tool reported completion")
        ]

    asyncio.run(scenario())


def test_runtime_cancel_waits_for_protocol_acknowledgement(fake_manifest, monkeypatch):
    runtime = ACPRuntime()
    monkeypatch.setattr(runtime, "_manifest", lambda: fake_manifest())
    registry = acp_runtime.get_process_registry()

    async def scenario():
        task = asyncio.create_task(
            runtime.execute_headless(
                "wait-for-cancel", execution_id="protocol-cancel", timeout_seconds=30
            )
        )
        for _ in range(200):
            if registry.get_status("protocol-cancel") is not None:
                break
            await asyncio.sleep(0.01)
        assert registry.get_status("protocol-cancel") is not None
        confirmed = await asyncio.to_thread(
            runtime.cancel_execution, "protocol-cancel", 2.0
        )
        assert confirmed is True
        with pytest.raises(HTTPException) as cancelled:
            await task
        assert cancelled.value.status_code == 499
        assert registry.was_terminated("protocol-cancel") is True

    asyncio.run(scenario())


def test_terminate_endpoint_prefers_runtime_cancellation(monkeypatch):
    runtime = SimpleNamespace(cancel_execution=lambda execution_id, timeout: True)
    registry = SimpleNamespace(
        terminate=lambda *_args: (_ for _ in ()).throw(
            AssertionError("signal fallback should not run")
        )
    )
    monkeypatch.setattr(chat_router, "get_runtime", lambda: runtime)
    monkeypatch.setattr(chat_router, "get_process_registry", lambda: registry)

    result = asyncio.run(chat_router.terminate_execution("protocol-execution"))

    assert result == {
        "status": "terminated",
        "execution_id": "protocol-execution",
        "method": "runtime",
    }


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


def test_protocol_sanitizer_redacts_secrets_at_arbitrary_json_depth():
    secret = "sk-deepcredential012345678901234"
    value = {"secret-key-" + secret: secret}
    for _ in range(80):
        value = {"nested": [value]}

    rendered = json.dumps(acp_runtime._sanitize_protocol_value(value))

    assert secret not in rendered
    assert "***REDACTED***" in rendered


def test_usage_update_rejects_non_finite_cost_and_wrong_session():
    connection = acp_runtime._ACPConnection(ACPManifest(("/bin/false",)))
    connection.session_id = "expected"
    state = acp_runtime._PromptState()
    with pytest.raises(RuntimeError, match="unexpected sessionId"):
        connection._handle_update(
            {
                "sessionId": "other",
                "update": {"sessionUpdate": "usage_update", "used": 1, "size": 2},
            },
            state,
        )
    with pytest.raises(RuntimeError, match="non-negative amount"):
        connection._handle_update(
            {
                "sessionId": "expected",
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 1,
                    "size": 2,
                    "cost": {"amount": float("nan"), "currency": "USD"},
                },
            },
            state,
        )


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
    checks = []
    live_state = importlib.import_module("agent_server.state")
    monkeypatch.setattr(live_state, "load_acp_manifest", lambda: checks.append(True))
    state = live_state.AgentState.__new__(live_state.AgentState)
    state.agent_runtime = "acp"
    state._check_claude_code = lambda: (_ for _ in ()).throw(
        AssertionError("Claude health fallback")
    )

    assert state._check_runtime_available() is True
    assert checks == [True]


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
