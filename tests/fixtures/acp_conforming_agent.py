"""Conforming ACP test agent implemented with the official Python SDK."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from acp import (
    PROTOCOL_VERSION,
    Agent,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestError,
    run_agent,
    text_block,
    update_agent_message,
)
from acp.interfaces import Client
from acp.schema import (
    AgentCapabilities,
    AudioContentBlock,
    ClientCapabilities,
    Cost,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    Implementation,
    McpCapabilities,
    PermissionOption,
    PromptCapabilities,
    ResourceContentBlock,
    SessionCapabilities,
    SessionCloseCapabilities,
    ToolCallUpdate,
    TextContentBlock,
    Usage,
    UsageUpdate,
)


class ConformingAgent(Agent):
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.connection: Client | None = None
        self.cancelled = asyncio.Event()
        self.next_session = 1

    def _record(self, event: str, **details: Any) -> None:
        if not self.args.events:
            return
        with Path(self.args.events).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **details}) + "\n")

    def on_connect(self, conn: Client) -> None:
        self.connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        self._record("initialize", protocol_version=protocol_version)
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=self.args.load,
                prompt_capabilities=PromptCapabilities(image=True, audio=False),
                mcp_capabilities=McpCapabilities(http=False, sse=False),
                session_capabilities=SessionCapabilities(close=SessionCloseCapabilities()),
            ),
            agent_info=Implementation(name="trinity-test-agent", version="1.0.0"),
        )

    async def new_session(self, cwd: str, mcp_servers=None, **kwargs: Any) -> NewSessionResponse:
        session_id = f"new-{self.next_session}"
        self.next_session += 1
        self._record("new", session_id=session_id, mcp_count=len(mcp_servers or []))
        return NewSessionResponse(session_id=session_id)

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers=None, **kwargs: Any
    ) -> LoadSessionResponse:
        self._record("load", session_id=session_id, mcp_count=len(mcp_servers or []))
        return LoadSessionResponse()

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            TextContentBlock
            | ImageContentBlock
            | AudioContentBlock
            | ResourceContentBlock
            | EmbeddedResourceContentBlock
        ],
        **kwargs: Any,
    ) -> PromptResponse:
        assert self.connection is not None
        self._record("prompt", session_id=session_id)
        text = "".join(block.text for block in prompt if getattr(block, "type", None) == "text")
        if text == "error":
            raise RequestError.internal_error("intentional test error")
        if text == "cost":
            await self.connection.session_update(
                session_id,
                UsageUpdate(
                    session_update="usage_update",
                    used=10,
                    size=100,
                    cost=Cost(amount=1.25, currency="USD"),
                ),
            )
        if text == "permission":
            options = [
                PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
            ]
            if not self.args.allow_only:
                options.append(
                    PermissionOption(option_id="deny", name="Deny", kind="reject_once")
                )
            result = await self.connection.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id="tool-1", title="Test tool"),
                options=options,
            )
            self._record(
                "permission",
                outcome=result.outcome.outcome,
                option_id=getattr(result.outcome, "option_id", None),
            )
        if text == "cancel":
            await self.cancelled.wait()
            return PromptResponse(stop_reason="cancelled")
        await self.connection.session_update(session_id, update_agent_message(text_block("chunk-one ")))
        await self.connection.session_update(session_id, update_agent_message(text_block("chunk-two")))
        return PromptResponse(
            stop_reason="end_turn",
            usage=Usage(total_tokens=5, input_tokens=3, output_tokens=2),
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self._record("cancel", session_id=session_id)
        self.cancelled.set()

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        self._record("close", session_id=session_id)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events")
    parser.add_argument("--load", action="store_true")
    parser.add_argument("--allow-only", action="store_true")
    return parser.parse_args()


async def main() -> None:
    agent = ConformingAgent(parse_args())
    try:
        await run_agent(agent, use_unstable_protocol=True)
    finally:
        agent._record("process_exit")


if __name__ == "__main__":
    asyncio.run(main())
