"""Codex MCP configuration tests (#1187 Phase F).

Codex reads MCP servers from ``$CODEX_HOME/config.toml``. Trinity writes that
file directly and merges, so:
  * the Trinity HTTP MCP server references the bearer token by ENV VAR and never
    persists the literal secret to disk,
  * template stdio servers are written as command + args,
  * re-running (agent restart) merges idempotently — no duplicate tables,
  * the dispatcher routes AGENT_RUNTIME=codex to the codex writer.
"""

from __future__ import annotations

import tomllib

import pytest

from agent_server.services import trinity_mcp  # noqa: E402


def test_codex_trinity_mcp_uses_env_var_not_literal_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    secret = "trinity_mcp_SUPERSECRETVALUE"

    assert trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", secret) is True

    raw = (tmp_path / "config.toml").read_text()
    cfg = tomllib.loads(raw)
    trinity = cfg["mcp_servers"]["trinity"]
    assert trinity["url"] == "http://mcp-server:8080/mcp"
    assert trinity["bearer_token_env_var"] == "TRINITY_MCP_API_KEY"
    # SECURITY: the literal token must NEVER appear in config.toml.
    assert secret not in raw


def test_codex_template_mcp_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    servers = {
        "github": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
        }
    }
    assert trinity_mcp._configure_codex_mcp_servers(servers) is True

    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    github = cfg["mcp_servers"]["github"]
    assert github["command"] == "npx"
    assert github["args"] == ["-y", "@modelcontextprotocol/server-github"]


def test_codex_mcp_config_merges_no_dup_on_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", "tok1")
    trinity_mcp._configure_codex_mcp_servers(
        {"github": {"command": "npx", "args": ["x"]}}
    )
    # Simulate a restart re-injecting Trinity MCP — must NOT duplicate.
    trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", "tok2")

    raw = (tmp_path / "config.toml").read_text()
    cfg = tomllib.loads(raw)
    assert set(cfg["mcp_servers"].keys()) == {"trinity", "github"}
    assert raw.count("[mcp_servers.trinity]") == 1
    assert raw.count("[mcp_servers.github]") == 1


def test_codex_template_server_env_table(tmp_path, monkeypatch):
    """A per-server env table serializes as a nested [mcp_servers.x.env] table
    and round-trips as valid TOML."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    servers = {
        "custom": {"command": "run-it", "env": {"FOO": "bar"}},
    }
    assert trinity_mcp._configure_codex_mcp_servers(servers) is True
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["mcp_servers"]["custom"]["command"] == "run-it"
    assert cfg["mcp_servers"]["custom"]["env"] == {"FOO": "bar"}


def test_codex_dispatch_routes_to_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    monkeypatch.setenv("TRINITY_MCP_URL", "http://mcp-server:8080/mcp")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "key")

    assert trinity_mcp.inject_trinity_mcp_if_configured() is True
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "trinity" in cfg["mcp_servers"]


# ---------------------------------------------------------------------------
# M1 — TOML writer hardening (#1187 review): a server name or value with
# special characters must NOT produce unparseable TOML that silently drops
# every server on the next merge.
# ---------------------------------------------------------------------------

def test_codex_server_name_with_space_is_valid_toml(tmp_path, monkeypatch):
    """A template MCP server whose name contains a space must serialize to a
    quoted table key that round-trips — not a raw `[mcp_servers.my server]`
    header that tomllib rejects."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    servers = {"my server": {"command": "run-it"}}

    assert trinity_mcp._configure_codex_mcp_servers(servers) is True

    raw = (tmp_path / "config.toml").read_text()
    cfg = tomllib.loads(raw)  # must not raise TOMLDecodeError
    assert cfg["mcp_servers"]["my server"]["command"] == "run-it"


def test_codex_server_name_with_dot_does_not_misnest(tmp_path, monkeypatch):
    """A dotted server name must be a single quoted key, not silently nested
    as `mcp_servers.a.b`."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    servers = {"a.b": {"command": "x"}}

    assert trinity_mcp._configure_codex_mcp_servers(servers) is True

    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "a.b" in cfg["mcp_servers"]
    assert cfg["mcp_servers"]["a.b"]["command"] == "x"


def test_codex_value_with_control_chars_is_escaped(tmp_path, monkeypatch):
    """A command/env value containing a newline/tab must be escaped so the
    file stays valid TOML and the value round-trips intact."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    servers = {"weird": {"command": "run", "env": {"K": "line1\nline2\ttab"}}}

    assert trinity_mcp._configure_codex_mcp_servers(servers) is True

    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert cfg["mcp_servers"]["weird"]["env"]["K"] == "line1\nline2\ttab"


def test_codex_malformed_config_is_backed_up_not_silently_reset(tmp_path, monkeypatch):
    """A malformed config.toml must be backed up (and logged), never silently
    swallowed — otherwise the next merge rewrites from {} and drops every
    previously-written server including the Trinity MCP wiring."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is { not valid toml [[[")

    # The upsert must still succeed and write the new server...
    assert trinity_mcp._configure_codex_mcp_servers(
        {"github": {"command": "npx"}}
    ) is True
    cfg = tomllib.loads(config_path.read_text())
    assert "github" in cfg["mcp_servers"]
    # ...and the corrupt original must be preserved for recovery, not lost.
    backups = list(tmp_path.glob("config.toml.corrupt*"))
    assert backups, "malformed config.toml was not backed up"


def test_codex_preserves_foreign_top_level_table_on_merge(tmp_path, monkeypatch):
    """If Codex (or a user) writes its own top-level scalars/tables to
    config.toml, a Trinity MCP merge must preserve them — not drop everything
    that isn't `mcp_servers`."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model = "gpt-5.1-codex"\n\n[history]\npersistence = "save-all"\n'
    )

    assert trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", "tok") is True

    cfg = tomllib.loads(config_path.read_text())
    assert cfg["model"] == "gpt-5.1-codex"
    assert cfg["history"]["persistence"] == "save-all"
    assert cfg["mcp_servers"]["trinity"]["url"] == "http://mcp-server:8080/mcp"


def test_codex_array_of_tables_preserved_not_corrupted(tmp_path, monkeypatch):
    """The hand-rolled TOML writer doesn't emit array-of-tables. Rather than
    silently stringify one in a pre-existing config (corruption), the merge must
    fail safe: leave the original file untouched (#1187 review).
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    original = (
        '[[shell_environment_policy.experimental]]\nname = "a"\n\n'
        '[[shell_environment_policy.experimental]]\nname = "b"\n'
    )
    config_path.write_text(original)

    # The merge bails (returns False) rather than rewriting a mangled file.
    assert trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", "tok") is False

    # The original config is preserved byte-for-byte and still parses with the
    # array-of-tables intact — NOT corrupted into stringified dicts.
    after = config_path.read_text()
    assert after == original
    rt = tomllib.loads(after)
    assert rt["shell_environment_policy"]["experimental"] == [
        {"name": "a"},
        {"name": "b"},
    ]


# ---------------------------------------------------------------------------
# Added coverage (#1187 follow-up): the template-server dispatcher, the TOML
# scalar/escape primitives, and the no-command / empty-input edges.
# ---------------------------------------------------------------------------

def test_configure_mcp_servers_dispatcher_routes_codex(tmp_path, monkeypatch):
    """configure_mcp_servers() (template servers, distinct from the Trinity-MCP
    injector) routes AGENT_RUNTIME=codex to the codex writer."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    assert trinity_mcp.configure_mcp_servers({"gh": {"command": "npx"}}) is True
    cfg = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "gh" in cfg["mcp_servers"]


def test_configure_mcp_servers_empty_is_noop_true(monkeypatch):
    """No servers → True without touching disk (the early-return guard)."""
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    assert trinity_mcp.configure_mcp_servers({}) is True


def test_acp_never_falls_through_to_claude_mcp(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "acp")
    monkeypatch.setenv("TRINITY_MCP_URL", "https://trinity.invalid/mcp")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "secret")
    monkeypatch.setattr(
        trinity_mcp,
        "_inject_claude_mcp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Claude fallback")),
    )
    monkeypatch.setattr(
        trinity_mcp,
        "_configure_claude_mcp_servers",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Claude fallback")),
    )

    assert trinity_mcp.inject_trinity_mcp_if_configured() is False
    assert trinity_mcp.configure_mcp_servers({"server": {"command": "npx"}}) is False


def test_configure_codex_skips_server_without_command(tmp_path, monkeypatch, caplog):
    """A template server with no command is skipped with a warning; when it is
    the only server, nothing is written and the call reports False (no servers
    configured)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    import logging

    caplog.set_level(logging.WARNING, logger="agent_server.services.trinity_mcp")
    result = trinity_mcp._configure_codex_mcp_servers({"broken": {"args": ["x"]}})
    assert result is False  # one server in, zero written → not all-empty input
    assert any("no command specified" in r.getMessage() for r in caplog.records)
    # Nothing was written for an all-skipped batch.
    assert not (tmp_path / "config.toml").exists()


def test_configure_codex_empty_input_returns_true(tmp_path, monkeypatch):
    """An empty server dict is a no-op success (len == 0 → True)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert trinity_mcp._configure_codex_mcp_servers({}) is True


# -- TOML scalar / escape primitives ---------------------------------------

def test_toml_escape_encodes_control_characters():
    """A control char (< 0x20, or DEL 0x7F) with no shorthand becomes a \\uXXXX
    escape so the file stays valid TOML."""
    out = trinity_mcp._toml_escape("a\x01b\x7fc")
    assert "\\u0001" in out
    assert "\\u007F" in out
    # Shorthand escapes still win for the common control chars.
    assert trinity_mcp._toml_escape("x\ty\nz") == "x\\ty\\nz"


def test_toml_scalar_bool_int_float():
    assert trinity_mcp._toml_scalar(True) == "true"
    assert trinity_mcp._toml_scalar(False) == "false"
    assert trinity_mcp._toml_scalar(7) == "7"
    assert trinity_mcp._toml_scalar(1.5) == "1.5"


def test_toml_scalar_list_of_scalars():
    assert trinity_mcp._toml_scalar(["a", "b"]) == '["a", "b"]'


def test_toml_scalar_rejects_dict():
    """A dict reaching _toml_scalar is unexpected nesting — it must raise rather
    than emit a stringified dict (which would corrupt the file)."""
    with pytest.raises(TypeError):
        trinity_mcp._toml_scalar({"k": "v"})


def test_toml_scalar_rejects_array_of_tables():
    with pytest.raises(TypeError):
        trinity_mcp._toml_scalar([{"name": "a"}])


def test_codex_preserves_foreign_bool_and_int_scalars(tmp_path, monkeypatch):
    """Foreign top-level bool/int settings survive a Trinity MCP merge
    round-trip (exercises the bool/int branches of the scalar writer)."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text("[tui]\nenabled = true\nmax_width = 120\n")

    assert trinity_mcp._inject_codex_mcp("http://mcp-server:8080/mcp", "tok") is True

    cfg = tomllib.loads(config_path.read_text())
    assert cfg["tui"]["enabled"] is True
    assert cfg["tui"]["max_width"] == 120
    assert cfg["mcp_servers"]["trinity"]["url"] == "http://mcp-server:8080/mcp"
