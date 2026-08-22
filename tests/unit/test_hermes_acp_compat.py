"""Contract tests for the pinned Hermes ACP compatibility entrypoint."""
from __future__ import annotations

import importlib.util
import json
import stat
import sys
from collections import deque
from pathlib import Path
from types import ModuleType, SimpleNamespace


COMPAT_PATH = (
    Path(__file__).parents[2]
    / "docker"
    / "acp-harnesses"
    / "hermes"
    / "hermes_compat.py"
)


def _load_compat():
    spec = importlib.util.spec_from_file_location("trinity_hermes_compat_test", COMPAT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_tool_completion_is_forwarded_once(monkeypatch):
    original_events = []
    sent_updates = []

    def original_factory(*_args):
        def callback(*args, **kwargs):
            original_events.append((args, kwargs))

        return callback

    events = ModuleType("acp_adapter.events")
    events.make_tool_progress_cb = original_factory
    events._send_update = lambda conn, session_id, loop, update: sent_updates.append(update)
    events._build_plan_update_from_todo_result = lambda _result: None
    server = ModuleType("acp_adapter.server")
    server.make_tool_progress_cb = original_factory
    tools = ModuleType("acp_adapter.tools")
    tools.build_tool_complete = lambda tool_id, name, **values: {
        "tool_id": tool_id,
        "name": name,
        **values,
    }
    package = ModuleType("acp_adapter")
    package.events = events
    package.server = server
    package.tools = tools
    monkeypatch.setitem(sys.modules, "acp_adapter", package)
    monkeypatch.setitem(sys.modules, "acp_adapter.events", events)
    monkeypatch.setitem(sys.modules, "acp_adapter.server", server)
    monkeypatch.setitem(sys.modules, "acp_adapter.tools", tools)

    compat = _load_compat()
    compat.install_tool_completion_bridge()
    call_ids = {"terminal": deque(["tc-1"])}
    call_meta = {"tc-1": {"args": {"command": "true"}, "snapshot": "before"}}
    callback = server.make_tool_progress_cb(
        "connection", "session", "loop", call_ids, call_meta
    )

    callback("tool.started", "terminal", "true", {"command": "true"})
    callback("tool.completed", "terminal", result={"success": True, "output": "ok"})

    assert len(original_events) == 1
    assert sent_updates == [
        {
            "tool_id": "tc-1",
            "name": "terminal",
            "result": "{'success': True, 'output': 'ok'}",
            "function_args": {"command": "true"},
            "snapshot": "before",
        }
    ]
    assert call_ids == {}
    assert call_meta == {}


def test_duplicate_or_unmatched_completion_is_ignored(monkeypatch):
    events = ModuleType("acp_adapter.events")
    events.make_tool_progress_cb = lambda *_args: lambda *_a, **_kw: None
    events._send_update = lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected update"))
    events._build_plan_update_from_todo_result = lambda _result: None
    server = ModuleType("acp_adapter.server")
    tools = ModuleType("acp_adapter.tools")
    tools.build_tool_complete = lambda *_args, **_kwargs: None
    package = ModuleType("acp_adapter")
    package.events = events
    package.server = server
    package.tools = tools
    monkeypatch.setitem(sys.modules, "acp_adapter", package)
    monkeypatch.setitem(sys.modules, "acp_adapter.events", events)
    monkeypatch.setitem(sys.modules, "acp_adapter.server", server)
    monkeypatch.setitem(sys.modules, "acp_adapter.tools", tools)

    compat = _load_compat()
    compat.install_tool_completion_bridge()
    callback = server.make_tool_progress_cb(
        SimpleNamespace(), "session", SimpleNamespace(), {}, {}
    )

    callback("tool.completed", "terminal", result="late")


def test_provider_config_uses_runtime_model_without_persisting_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "gemini-test-model")
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-written")

    compat = _load_compat()
    config_path = compat.configure_provider()
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert data["model"] == {
        "provider": "gemini",
        "default": "gemini-test-model",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    }
    assert "must-not-be-written" not in config_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_path.parent.stat().st_mode) == 0o700


def test_terminal_auth_is_removed_but_provider_auth_is_retained(monkeypatch):
    provider = SimpleNamespace(type="agent", id="gemini")
    terminal = SimpleNamespace(type="terminal", id="hermes-setup")
    server = ModuleType("acp_adapter.server")
    server.build_auth_methods = lambda: [provider, terminal]
    package = ModuleType("acp_adapter")
    package.server = server
    monkeypatch.setitem(sys.modules, "acp_adapter", package)
    monkeypatch.setitem(sys.modules, "acp_adapter.server", server)

    compat = _load_compat()
    compat.install_auth_capability_bridge()

    assert server.build_auth_methods() == [provider]


def test_cancelled_none_response_is_normalized(monkeypatch):
    class FakeAgent:
        def run_conversation(self, *_args, **_kwargs):
            return {"final_response": None, "interrupted": True}

    run_agent = ModuleType("run_agent")
    run_agent.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", run_agent)

    compat = _load_compat()
    compat.install_cancel_response_bridge()

    assert FakeAgent().run_conversation() == {
        "final_response": "",
        "interrupted": True,
    }
