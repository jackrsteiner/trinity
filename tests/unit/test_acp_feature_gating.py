from pathlib import Path

import pytest

from agent_server.services.acp_runtime import ACPRuntime
from services.task_execution_service import gate_system_prompt


def test_acp_static_features_are_conservative_before_negotiation():
    capabilities = ACPRuntime.capabilities()
    assert capabilities.chat_continuity is True
    assert capabilities.mcp_support is True
    assert capabilities.session_load is False
    assert capabilities.model_selection is False
    assert capabilities.system_prompt is False
    assert capabilities.cost_reporting == "unavailable"
    assert capabilities.negotiated is False


def test_trinity_system_prompt_is_disabled_not_emulated_for_acp():
    assert gate_system_prompt("acp", "platform identity") is None
    assert gate_system_prompt("claude-code", "platform identity") == "platform identity"


def test_sync_chat_path_gates_the_platform_prompt():
    """BOTH sync-chat prompt writers must run through gate_system_prompt.

    The /chat path (MCP chat_with_agent's default sequential mode) sets the
    platform prompt unconditionally; ungated, every sync turn to an ACP agent
    409s at the runtime. Source-level guard (the frontend gating spec's idiom)
    because run_chat_turn is not unit-invocable without a full backend.
    """
    source = (
        Path(__file__).parents[2]
        / "src"
        / "backend"
        / "services"
        / "chat_execution_service.py"
    ).read_text()
    assert "gate_system_prompt" in source, (
        "chat_execution_service must import gate_system_prompt"
    )
    # One gated call per prompt writer: the compose path and the fallback path.
    assert source.count("payload[\"system_prompt\"] = gate_system_prompt(") == 2, (
        "every payload['system_prompt'] writer in the sync-chat path must be "
        "wrapped in gate_system_prompt"
    )
    assert "payload[\"system_prompt\"] = compose_system_prompt(" not in source
    assert "payload[\"system_prompt\"] = get_platform_system_prompt(" not in source


def test_acp_is_gated_out_of_session_tab_resume():
    """The resumable-turn engine must not pass resume_session_id to ACP agents:
    the static capability is session_tab_resume=False, and the engine's cold
    retry only fires on the Claude JSONL-missing error — an ACP 409 would
    permanently break the thread."""
    from services.session_turn_service import RUNTIMES_WITHOUT_SESSION_TAB_RESUME

    assert "acp" in RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    assert ACPRuntime.capabilities().session_tab_resume is False


def test_capabilities_snapshot_fails_open_on_unknown_runtime(monkeypatch):
    """/health and the session/model reads must degrade, never 500, when the
    runtime cannot be constructed — while execution paths keep failing loud."""
    from agent_server.services import runtime_adapter

    monkeypatch.setenv("AGENT_RUNTIME", "not-a-runtime")
    snapshot = runtime_adapter.get_capabilities_snapshot()
    assert snapshot.model_selection is True  # legacy pre-capability behavior
    assert snapshot.cost_reporting == "estimated"
    with pytest.raises(ValueError):
        runtime_adapter.get_runtime()


def test_capabilities_snapshot_fails_open_on_missing_acp_envelope(monkeypatch):
    from agent_server.services import acp_runtime as acp_module
    from agent_server.services import runtime_adapter

    monkeypatch.setenv("AGENT_RUNTIME", "acp")
    monkeypatch.delenv("AGENT_RUNTIME_COMMAND", raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_ARGS", raising=False)
    monkeypatch.setattr(acp_module, "_acp_runtime", None)  # defeat the singleton
    snapshot = runtime_adapter.get_capabilities_snapshot()
    assert snapshot.model_selection is True
    with pytest.raises(Exception):
        runtime_adapter.get_runtime()
