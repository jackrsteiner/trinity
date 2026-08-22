"""Compatibility entrypoint for Hermes Agent 0.19.0's ACP tool lifecycle.

Hermes emits a reliable ``tool.completed`` progress callback immediately after
each tool finishes, but its ACP adapter ignores that event and tries to rebuild
completion notifications from the following agent-loop step. Some providers,
including Gemini, can finish the turn without that reconstruction succeeding.
Forward the direct callback and leave all other Hermes behavior untouched.
"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
from typing import Any, Callable


def configure_provider() -> Path:
    """Materialize a key-free Hermes provider config for this derived image."""
    config_dir = Path.home() / ".hermes"
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    config_path = config_dir / "config.yaml"

    if os.getenv("TRINITY_HERMES_AUTOCONFIG", "1") == "0" and config_path.exists():
        return config_path

    model = (
        os.getenv("ACP_MODEL")
        or os.getenv("AGENT_RUNTIME_MODEL")
        or "gemini-3.7-flash"
    )
    provider = os.getenv("ACP_PROVIDER", "gemini")
    base_url = os.getenv(
        "ACP_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
    )
    # JSON is valid YAML. Writing a structured document avoids quoting bugs and
    # intentionally stores no credential value; Hermes reads GEMINI_API_KEY
    # directly from the process environment injected by Trinity.
    config_path.write_text(
        json.dumps(
            {
                "model": {
                    "provider": provider,
                    "default": model,
                    "base_url": base_url,
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


def install_tool_completion_bridge() -> None:
    """Teach the pinned Hermes ACP adapter to forward direct completions."""
    from acp_adapter import events, server, tools

    original_factory = events.make_tool_progress_cb

    def make_tool_progress_cb(
        conn: Any,
        session_id: str,
        loop: Any,
        tool_call_ids: dict,
        tool_call_meta: dict,
        edit_approval_policy_getter: Callable | None = None,
    ) -> Callable:
        original_callback = original_factory(
            conn,
            session_id,
            loop,
            tool_call_ids,
            tool_call_meta,
            edit_approval_policy_getter,
        )

        def callback(
            event_type: str,
            name: str | None = None,
            preview: str | None = None,
            args: Any = None,
            **kwargs: Any,
        ) -> None:
            if event_type != "tool.completed":
                original_callback(event_type, name, preview, args, **kwargs)
                return

            queue = tool_call_ids.get(name or "")
            if isinstance(queue, str):
                queue = deque([queue])
                tool_call_ids[name] = queue
            if not name or not queue:
                return

            tool_call_id = queue.popleft()
            meta = tool_call_meta.pop(tool_call_id, {})
            update = tools.build_tool_complete(
                tool_call_id,
                name,
                result=str(kwargs["result"]) if kwargs.get("result") is not None else None,
                function_args=meta.get("args"),
                snapshot=meta.get("snapshot"),
            )
            events._send_update(conn, session_id, loop, update)
            if name == "todo":
                plan_update = events._build_plan_update_from_todo_result(kwargs.get("result"))
                if plan_update is not None:
                    events._send_update(conn, session_id, loop, plan_update)
            if not queue:
                tool_call_ids.pop(name, None)

        return callback

    # server.py imports the factory into its own module namespace.
    server.make_tool_progress_cb = make_tool_progress_cb


def main() -> None:
    configure_provider()
    install_tool_completion_bridge()
    from acp_adapter.entry import main as hermes_main

    hermes_main()


if __name__ == "__main__":
    main()
