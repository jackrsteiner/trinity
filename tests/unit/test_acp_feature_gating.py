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
