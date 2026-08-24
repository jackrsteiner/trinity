"""Harness-neutral Agent Client Protocol runtime.

The official ``agent-client-protocol`` SDK owns schema models, JSON-RPC,
framing, stdio transport, and version negotiation. This module only coordinates
the standard ACP lifecycle and maps typed protocol events into Trinity's neutral
runtime result types.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import subprocess
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from fastapi import HTTPException

from acp import PROTOCOL_VERSION, RequestError, spawn_agent_process, text_block
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    ClientCapabilities,
    CreateElicitationResponse,
    DeclineElicitationResponse,
    DeniedOutcome,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    PermissionOption,
    ReadTextFileResponse,
    RequestPermissionResponse,
    SseMcpServer,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.credential_sanitizer import sanitize_dict, sanitize_text
from .acp_launch import ACPLaunchConfig, load_acp_launch_config
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities

logger = logging.getLogger(__name__)

PermissionResolver = Callable[
    [str, ToolCallUpdate, List[PermissionOption]],
    Optional[str] | Awaitable[Optional[str]],
]

_INITIALIZE_TIMEOUT_SECONDS = 15
_CANCEL_GRACE_SECONDS = 5
_SHUTDOWN_TIMEOUT_SECONDS = 2
_STDERR_LIMIT_BYTES = 64 * 1024

# The same JSON file Claude's read-only hook and Codex's sandbox resolver
# consume (``~/.trinity/read-only-config.json`` → ``enabled``). Module-level so
# tests can repoint it.
_READ_ONLY_CONFIG = Path("/home/developer") / ".trinity" / "read-only-config.json"

# ACP option kinds that express a denial through the protocol's own option
# mechanism. Preferred over DeniedOutcome("cancelled"), which some agents
# treat as "the whole turn was cancelled" rather than "this action was denied".
_REJECT_OPTION_KINDS = frozenset({"reject_once", "reject_always"})


class ACPRuntimeError(RuntimeError):
    """A protocol, lifecycle, or process failure in the generic ACP adapter."""


class ACPFeatureUnavailable(HTTPException, ACPRuntimeError):
    """A caller requested a feature the ACP agent did not advertise.

    Doubles as an ``HTTPException(409)`` (the ``ResumeLockBusy`` pattern) so the
    agent-server routes surface a legible refusal instead of a generic 500, and
    the backend classifies it as an agent-side error — never AUTH (503), which
    would feed the dispatch breaker a false signal.
    """

    def __init__(self, detail: str) -> None:
        HTTPException.__init__(self, status_code=409, detail=detail)

    def __str__(self) -> str:  # keep pytest.raises(match=...) on the detail text
        return self.detail


def _read_only_enabled() -> bool:
    """True when the backend has put this agent in read-only mode.

    Loader semantics mirror Codex's ``_is_read_only`` (CSO #1187 finding 3:
    the unreadable/corrupt-file case must fail the same direction across
    runtimes — open, with a WARNING). What differs is what an *enabled* flag
    means here: ACP has no portable enforcement channel at all, so the runtime
    REFUSES the turn rather than running unenforced (see
    ``_refuse_if_read_only``).
    """
    try:
        raw = _READ_ONLY_CONFIG.read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "[ACP] read-only config unreadable (%s); treating as not read-only", exc
        )
        return False
    try:
        return bool(json.loads(raw).get("enabled"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "[ACP] read-only config malformed (%s); treating as not read-only", exc
        )
        return False


def _refuse_if_read_only() -> None:
    """Fail closed on read-only mode.

    Claude enforces read-only via hooks and Codex via its sandbox; ACP v1 has
    no portable equivalent, and running the turn anyway would present the
    owner's read-only toggle as active while enforcing nothing. Refusing is the
    only non-silent option (ADR 0002 §3: never emulate, never silently drop).
    """
    if _read_only_enabled():
        raise ACPFeatureUnavailable(
            "This agent is in read-only mode, which the generic ACP runtime "
            "cannot enforce. Refusing to execute unenforced — disable read-only "
            "mode or use a runtime with native read-only enforcement."
        )


class _AsyncioProcessHandle:
    """Minimal synchronous facade used by Trinity's existing process registry."""

    def __init__(self, process: asyncio.subprocess.Process):
        self._process = process
        self.pid = process.pid

    @property
    def returncode(self) -> Optional[int]:
        return self._process.returncode

    def poll(self) -> Optional[int]:
        return self._process.returncode

    def send_signal(self, sig: int) -> None:
        self._process.send_signal(sig)

    def kill(self) -> None:
        self._process.kill()

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._process.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.02)
        return self._process.returncode


@dataclass
class _TurnCollector:
    permission_resolver: Optional[PermissionResolver]
    execution_id: Optional[str]
    response_parts: List[str] = field(default_factory=list)
    execution_log: List[ExecutionLogEntry] = field(default_factory=list)
    raw_messages: List[Dict[str, Any]] = field(default_factory=list)
    context_used: int = 0
    context_size: int = 0
    cost_usd: Optional[float] = None
    stderr: List[str] = field(default_factory=list)

    def _publish(self, payload: Dict[str, Any]) -> None:
        sanitized = sanitize_dict(payload)
        self.raw_messages.append(sanitized)
        if self.execution_id:
            get_process_registry().publish_log_entry(self.execution_id, sanitized)

    def reset_turn(self, execution_id: Optional[str]) -> None:
        """Start a fresh turn on a persistent chat session.

        Clears every per-turn quantity — including ``cost_usd``, which is a
        billing amount: leaving it set would re-add turn 1's cost to the
        session total on every later turn that emits no ``UsageUpdate``.
        ``context_used``/``context_size`` deliberately survive: they describe
        the live session's window occupancy (last-known semantics), not this
        turn's spend.
        """
        self.execution_id = execution_id
        self.response_parts.clear()
        self.execution_log.clear()
        self.raw_messages.clear()
        self.cost_usd = None

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: List[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self._publish(
            {
                "type": "acp_permission_request",
                "session_id": session_id,
                "tool_call": tool_call.model_dump(by_alias=True, exclude_none=True),
                "options": [option.model_dump(by_alias=True, exclude_none=True) for option in options],
            }
        )
        option_id: Optional[str] = None
        if self.permission_resolver is not None:
            decision = self.permission_resolver(session_id, tool_call, options)
            option_id = await decision if inspect.isawaitable(decision) else decision
        valid_ids = {option.option_id for option in options}
        if option_id not in valid_ids:
            # Default deny: prefer the agent's own reject-kind option so the
            # denial is expressed through the protocol's option mechanism.
            # DeniedOutcome("cancelled") is the last resort — some agents read
            # it as "the turn was cancelled" rather than "this action was
            # denied", which can abort the whole prompt.
            option_id = next(
                (
                    option.option_id
                    for option in options
                    if option.kind in _REJECT_OPTION_KINDS
                ),
                None,
            )
        if option_id in valid_ids:
            outcome = AllowedOutcome(outcome="selected", option_id=option_id)
        else:
            outcome = DeniedOutcome(outcome="cancelled")
        self._publish(
            {
                "type": "acp_permission_response",
                "session_id": session_id,
                "outcome": outcome.model_dump(by_alias=True, exclude_none=True),
            }
        )
        return RequestPermissionResponse(outcome=outcome)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._publish(
            {
                "type": "acp_session_update",
                "session_id": session_id,
                "update": update.model_dump(by_alias=True, exclude_none=True),
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        if isinstance(update, AgentMessageChunk):
            if isinstance(update.content, TextContentBlock):
                self.response_parts.append(update.content.text)
            return
        if isinstance(update, ToolCallStart):
            self.execution_log.append(
                ExecutionLogEntry(
                    id=update.tool_call_id,
                    type="tool_use",
                    tool=update.title,
                    input=update.raw_input if isinstance(update.raw_input, dict) else None,
                    timestamp=now,
                )
            )
            return
        if isinstance(update, ToolCallProgress) and update.status in {"completed", "failed"}:
            output = None
            if update.raw_output is not None:
                output = sanitize_text(
                    update.raw_output
                    if isinstance(update.raw_output, str)
                    else json.dumps(update.raw_output, default=str)
                )
            self.execution_log.append(
                ExecutionLogEntry(
                    id=update.tool_call_id,
                    type="tool_result",
                    tool=update.title or "tool",
                    output=output,
                    success=update.status == "completed",
                    timestamp=now,
                )
            )
            return
        if isinstance(update, UsageUpdate):
            self.context_used = update.used
            self.context_size = update.size
            if update.cost is not None and update.cost.currency.upper() == "USD":
                self.cost_usd = update.cost.amount

    async def write_text_file(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, **kwargs: Any) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, **kwargs: Any) -> None:
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(self, **kwargs: Any) -> CreateElicitationResponse:
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: Dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)

    def on_connect(self, conn: Any) -> None:
        return None


@dataclass
class _ACPSession:
    manager: Any
    connection: Any
    process: asyncio.subprocess.Process
    collector: _TurnCollector
    session_id: str
    capabilities: RuntimeCapabilities
    raw_capabilities: Dict[str, Any]
    stderr_task: Optional[asyncio.Task]
    closed: bool = False


class ACPRuntime(AgentRuntime):
    """Generic ACP v1 client adapter with no agent/provider-specific behavior."""

    def __init__(
        self,
        launch: ACPLaunchConfig,
        *,
        env_provider: Callable[[], Mapping[str, str]] = build_execution_env,
        cwd: Optional[Path] = None,
        permission_resolver: Optional[PermissionResolver] = None,
    ) -> None:
        self._launch = launch
        self._env_provider = env_provider
        self._cwd = (cwd or Path.cwd()).resolve()
        self._permission_resolver = permission_resolver
        self._mcp_servers: List[Any] = []
        self._chat_session: Optional[_ACPSession] = None
        self._active_prompts: Dict[str, Tuple[_ACPSession, asyncio.Event]] = {}
        self._capabilities = self.capabilities()
        self._raw_capabilities: Dict[str, Any] = {}

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            chat_continuity=True,
            mcp_support=True,
            cost_reporting="unavailable",
        )

    def get_capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    @property
    def raw_capabilities(self) -> Dict[str, Any]:
        return dict(self._raw_capabilities)

    def is_available(self) -> bool:
        return self._launch.is_available()

    def get_default_model(self) -> str:
        return ""

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        try:
            block = mcp_servers.get("mcpServers", mcp_servers)
            if not isinstance(block, dict):
                return False
            converted: List[Any] = []
            for name, config in block.items():
                if not isinstance(name, str) or not isinstance(config, dict):
                    return False
                transport = config.get("type", "stdio")
                if transport == "stdio":
                    env = config.get("env", {}) or {}
                    if not isinstance(env, dict):
                        return False
                    converted.append(
                        McpServerStdio(
                            name=name,
                            command=config["command"],
                            args=list(config.get("args", [])),
                            env=[EnvVariable(name=key, value=value) for key, value in env.items()],
                        )
                    )
                elif transport in {"http", "sse"}:
                    headers = config.get("headers", {}) or {}
                    header_models = [HttpHeader(name=key, value=value) for key, value in headers.items()]
                    model = HttpMcpServer if transport == "http" else SseMcpServer
                    converted.append(model(type=transport, name=name, url=config["url"], headers=header_models))
                else:
                    return False
            self._mcp_servers = converted
            return True
        except (KeyError, TypeError, ValueError):
            logger.warning("Invalid generic ACP MCP configuration", exc_info=True)
            return False

    @staticmethod
    def _derive_capabilities(raw: Any) -> RuntimeCapabilities:
        prompt = raw.prompt_capabilities
        mcp = raw.mcp_capabilities
        return RuntimeCapabilities(
            chat_continuity=True,
            session_tab_resume=bool(raw.load_session),
            session_load=bool(raw.load_session),
            mcp_support=True,
            prompt_images=bool(prompt and prompt.image),
            prompt_audio=bool(prompt and prompt.audio),
            cost_reporting="unavailable",
            negotiated=True,
        )

    def _supported_mcp_servers(self, raw: Any) -> List[Any]:
        mcp = raw.mcp_capabilities
        result: List[Any] = []
        for server in self._mcp_servers:
            if isinstance(server, McpServerStdio):
                result.append(server)
            elif isinstance(server, HttpMcpServer) and mcp and mcp.http:
                result.append(server)
            elif isinstance(server, SseMcpServer) and mcp and mcp.sse:
                result.append(server)
        return result

    async def _drain_stderr(self, process: asyncio.subprocess.Process, collector: _TurnCollector) -> None:
        """Capture up to ``_STDERR_LIMIT_BYTES`` of agent stderr, then DISCARD.

        The drain must never stop reading: a chatty agent that fills the kernel
        pipe buffer blocks on its next stderr write and wedges the prompt
        mid-turn. Past the cap, bytes are read and dropped so backpressure can
        never build. A single over-long line (asyncio stream limit) degrades to
        a raw byte drain instead of killing the task.
        """
        if process.stderr is None:
            return
        total = 0
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                if total >= _STDERR_LIMIT_BYTES:
                    continue  # keep the pipe flowing; discard past the cap
                total += len(line)
                text = sanitize_text(line.decode("utf-8", errors="replace").rstrip())
                if text:
                    collector.stderr.append(text)
        except (ValueError, asyncio.LimitOverrunError):
            # readline() raises when one line exceeds the stream limit; fall
            # back to draining raw bytes so the child never blocks on stderr.
            with suppress(Exception):
                while await process.stderr.read(_STDERR_LIMIT_BYTES):
                    pass

    async def _open_session(
        self,
        *,
        execution_id: Optional[str],
        resume_session_id: Optional[str] = None,
    ) -> _ACPSession:
        collector = _TurnCollector(self._permission_resolver, execution_id)
        manager = spawn_agent_process(
            collector,
            self._launch.command,
            *self._launch.args,
            env=self._env_provider(),
            cwd=self._cwd,
            transport_kwargs={"shutdown_timeout": _SHUTDOWN_TIMEOUT_SECONDS},
            # SDK 0.12.1 marks a handful of newer methods "unstable"
            # (session/close — which _close_session uses when advertised —
            # plus elicitation and fork/resume) and warns per call unless this
            # flag is set; it changes no wire behavior for the stable methods.
            # Deterministic because the SDK is exact-pinned in the base image
            # and tests/requirements-test.txt.
            use_unstable_protocol=True,
        )
        try:
            connection, process = await manager.__aenter__()
            stderr_task = asyncio.create_task(self._drain_stderr(process, collector))
            initialized = await asyncio.wait_for(
                connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(
                        name="trinity",
                        title="Trinity",
                        version="2.0.0",
                    ),
                ),
                timeout=_INITIALIZE_TIMEOUT_SECONDS,
            )
            if initialized.protocol_version != PROTOCOL_VERSION:
                raise ACPRuntimeError(
                    "ACP protocol version mismatch: "
                    f"client={PROTOCOL_VERSION}, agent={initialized.protocol_version}"
                )
            raw_caps_model = initialized.agent_capabilities
            if raw_caps_model is None:
                raise ACPRuntimeError("ACP initialize response omitted agent capabilities")
            capabilities = self._derive_capabilities(raw_caps_model)
            raw_capabilities = raw_caps_model.model_dump(by_alias=True, exclude_none=True)
            self._capabilities = capabilities
            self._raw_capabilities = raw_capabilities
            mcp_servers = self._supported_mcp_servers(raw_caps_model)
            if resume_session_id:
                if not capabilities.session_load:
                    raise ACPFeatureUnavailable("ACP agent did not advertise session/load")
                await connection.load_session(
                    cwd=str(self._cwd),
                    session_id=resume_session_id,
                    mcp_servers=mcp_servers,
                )
                session_id = resume_session_id
            else:
                created = await connection.new_session(cwd=str(self._cwd), mcp_servers=mcp_servers)
                session_id = created.session_id
            return _ACPSession(
                manager=manager,
                connection=connection,
                process=process,
                collector=collector,
                session_id=session_id,
                capabilities=capabilities,
                raw_capabilities=raw_capabilities,
                stderr_task=stderr_task,
            )
        except BaseException:
            with suppress(Exception):
                await manager.__aexit__(None, None, None)
            raise

    async def _close_session(self, session: _ACPSession) -> None:
        if session.closed:
            return
        session.closed = True
        session_caps = session.raw_capabilities.get("sessionCapabilities") or {}
        if session_caps.get("close") is not None:
            with suppress(Exception):
                await session.connection.close_session(session.session_id)
        with suppress(Exception):
            await session.manager.__aexit__(None, None, None)
        if session.stderr_task is not None:
            with suppress(Exception):
                await asyncio.wait_for(session.stderr_task, timeout=1)

    async def _prompt(
        self,
        session: _ACPSession,
        prompt: str,
        *,
        execution_id: Optional[str],
        images: Optional[List[Dict]] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        blocks: List[Any] = [text_block(prompt)]
        if images:
            if not session.capabilities.prompt_images:
                raise ACPFeatureUnavailable("ACP agent did not advertise image prompts")
            for image in images:
                blocks.append(
                    ImageContentBlock(
                        type="image",
                        mime_type=image["media_type"],
                        data=image["data"],
                    )
                )
        done = asyncio.Event()
        if execution_id:
            self._active_prompts[execution_id] = (session, done)
            get_process_registry().register(
                execution_id,
                _AsyncioProcessHandle(session.process),
                {"type": "acp", "session_id": session.session_id},
            )
        started = time.monotonic()
        try:
            request = session.connection.prompt(session_id=session.session_id, prompt=blocks)
            response = (
                await asyncio.wait_for(request, timeout=timeout_seconds)
                if timeout_seconds is not None
                else await request
            )
        except asyncio.TimeoutError as exc:
            with suppress(Exception):
                await session.connection.cancel(session_id=session.session_id)
            raise HTTPException(status_code=504, detail="ACP prompt timed out") from exc
        finally:
            done.set()
            if execution_id:
                self._active_prompts.pop(execution_id, None)
                get_process_registry().unregister(execution_id)
        usage = response.usage
        if session.collector.cost_usd is not None:
            self._capabilities = RuntimeCapabilities(
                **{
                    **self._capabilities.to_dict(),
                    "cost_reporting": "native",
                }
            )
        metadata = ExecutionMetadata(
            cost_usd=session.collector.cost_usd,
            duration_ms=int((time.monotonic() - started) * 1000),
            tool_count=sum(1 for item in session.collector.execution_log if item.type == "tool_use"),
            session_id=session.session_id,
            execution_id=execution_id,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=(usage.cached_read_tokens or 0) if usage else 0,
            cache_creation_tokens=(usage.cached_write_tokens or 0) if usage else 0,
            context_window=session.collector.context_size,
            # "cancelled" and "refusal" are the two stop reasons that mean the
            # turn did NOT produce the requested answer. Budget stops
            # (max_tokens / max_turn_requests) still return partial text and
            # stay "success", matching how CLI runtimes report truncation.
            status="success" if response.stop_reason not in {"cancelled", "refusal"} else "error",
            error_code=None if response.stop_reason not in {"cancelled", "refusal"} else "AGENT_ERROR",
        )
        return (
            "".join(session.collector.response_parts),
            list(session.collector.execution_log),
            metadata,
            list(session.collector.raw_messages),
        )

    @asynccontextmanager
    async def _map_protocol_errors(self):
        """Convert protocol/lifecycle failures into typed HTTP errors.

        The routers unpack the runtime result directly, so anything that is not
        an ``HTTPException`` surfaces as a bare 500 with no detail. Mapping here
        mirrors the Codex precedent: agent/protocol failures → **502** (which
        the backend classifies as AGENT_ERROR), timeouts → 504 (raised in
        ``_prompt``), capability refusals → 409 (``ACPFeatureUnavailable``).
        Deliberately NEVER 503/429 — the backend reads those as AUTH/rate
        signals (dispatch breaker, SUB-003), and a harness-neutral adapter has
        no portable way to prove an auth failure.
        """
        try:
            yield
        except HTTPException:
            raise  # ACPFeatureUnavailable (409) and the 504 timeout pass through
        except (ACPRuntimeError, RequestError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"ACP agent error: {sanitize_text(str(exc))}",
            ) from exc
        except Exception as exc:  # malformed SDK payloads, connection loss, …
            logger.exception("[ACP] unexpected protocol-layer failure")
            raise HTTPException(
                status_code=502,
                detail=f"ACP agent error: {type(exc).__name__}: {sanitize_text(str(exc))}",
            ) from exc

    @staticmethod
    def _reject_nonportable_controls(
        *,
        model: Optional[str],
        system_prompt: Optional[str],
        allowed_tools: Optional[List[str]] = None,
        max_turns: Optional[int] = None,
    ) -> None:
        unsupported = []
        if model:
            unsupported.append("model selection")
        if system_prompt:
            unsupported.append("system prompts")
        if allowed_tools:
            unsupported.append("tool restrictions")
        if max_turns is not None:
            unsupported.append("max turns")
        if unsupported:
            raise ACPFeatureUnavailable(
                "ACP v1 does not portably support: " + ", ".join(unsupported)
            )

    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        self._reject_nonportable_controls(model=model, system_prompt=system_prompt)
        _refuse_if_read_only()
        async with self._map_protocol_errors():
            if not continue_session and self._chat_session is not None:
                await self._close_session(self._chat_session)
                self._chat_session = None
            if self._chat_session is None or self._chat_session.closed:
                self._chat_session = await self._open_session(execution_id=execution_id)
            self._chat_session.collector.reset_turn(execution_id)
            return await self._prompt(self._chat_session, prompt, execution_id=execution_id)

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], ExecutionMetadata, str]:
        # Second tuple element is the raw sanitized message stream (the
        # /api/task contract every runtime honors), not the typed
        # ExecutionLogEntry list — that one rides inside the stream.
        self._reject_nonportable_controls(
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
        )
        _refuse_if_read_only()
        async with self._map_protocol_errors():
            session = await self._open_session(
                execution_id=execution_id,
                resume_session_id=resume_session_id,
            )
            try:
                response, log, metadata, raw_messages = await self._prompt(
                    session,
                    prompt,
                    execution_id=execution_id,
                    images=images,
                    timeout_seconds=timeout_seconds,
                )
                return response, raw_messages, metadata, session.session_id
            finally:
                await self._close_session(session)

    async def cancel_execution(self, execution_id: str) -> bool:
        active = self._active_prompts.get(execution_id)
        if active is None:
            return False
        session, done = active
        try:
            # Bounded: a wedged agent must not hang the terminate endpoint.
            # An undeliverable protocol cancel returns False so the caller
            # falls back to the process registry's OS-signal termination.
            await asyncio.wait_for(
                session.connection.cancel(session_id=session.session_id),
                timeout=_CANCEL_GRACE_SECONDS,
            )
        except Exception as exc:  # incl. asyncio.TimeoutError
            logger.warning(
                "[ACP] protocol cancel for %s failed (%s); falling back to signal termination",
                execution_id,
                exc,
            )
            return False
        get_process_registry().mark_terminated(execution_id)
        try:
            await asyncio.wait_for(done.wait(), timeout=_CANCEL_GRACE_SECONDS)
        except asyncio.TimeoutError:
            await self._close_session(session)
        return True

    async def reset_chat(self) -> None:
        """Close the persistent interactive session on chat reset.

        Trinity's transcript reset must reach the runtime: unlike the CLI
        runtimes (stateless between invocations), the ACP chat session is a
        live child process holding the full prior context — "New Chat" without
        this would silently carry it forward (ADR 0002 §7).
        """
        if self._chat_session is not None:
            await self._close_session(self._chat_session)
            self._chat_session = None

    async def close(self) -> None:
        sessions = {id(session): session for session, _ in self._active_prompts.values()}
        if self._chat_session is not None:
            sessions[id(self._chat_session)] = self._chat_session
        for session in sessions.values():
            await self._close_session(session)
        self._active_prompts.clear()
        self._chat_session = None


_acp_runtime: Optional[ACPRuntime] = None


def get_acp_runtime() -> ACPRuntime:
    global _acp_runtime
    if _acp_runtime is None:
        _acp_runtime = ACPRuntime(load_acp_launch_config())
    return _acp_runtime
