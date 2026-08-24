from __future__ import annotations

import pytest

from agent_server.services.acp_launch import (
    ACPLaunchConfig,
    ACPLaunchConfigError,
    load_acp_launch_config,
)


def test_loads_shell_free_command_vector():
    config = load_acp_launch_config(
        {
            "AGENT_RUNTIME_COMMAND": "example-agent",
            "AGENT_RUNTIME_ARGS": '["serve", "--acp"]',
        }
    )
    assert config == ACPLaunchConfig("example-agent", ("serve", "--acp"))


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"AGENT_RUNTIME_COMMAND": "agent", "AGENT_RUNTIME_ARGS": "not-json"},
        {"AGENT_RUNTIME_COMMAND": "agent", "AGENT_RUNTIME_ARGS": '{"arg": true}'},
        {"AGENT_RUNTIME_COMMAND": "agent", "AGENT_RUNTIME_ARGS": "[1]"},
    ],
)
def test_rejects_missing_or_malformed_launch_configuration(env):
    with pytest.raises(ACPLaunchConfigError):
        load_acp_launch_config(env)


def test_does_not_parse_shell_syntax():
    config = load_acp_launch_config(
        {
            "AGENT_RUNTIME_COMMAND": "agent --flag",
            "AGENT_RUNTIME_ARGS": '["literal argument with spaces"]',
        }
    )
    assert config.command == "agent --flag"
    assert config.args == ("literal argument with spaces",)
