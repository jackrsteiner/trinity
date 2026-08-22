"""Generic Agent Client Protocol (ACP) runtime over stdio.

The runtime deliberately knows nothing about individual harnesses. A derived
image supplies one immutable root-owned manifest and launcher; Trinity speaks
newline-delimited JSON-RPC to that launcher and maps the common ACP surface to
``AgentRuntime``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..acp_manifest import (
    ACP_MANIFEST,
    ACPManifest,
    load_acp_manifest as _load_manifest,
)
from ..model_context import resolve_context_window
from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.credential_sanitizer import (
    sanitize_subprocess_line,
    sanitize_text,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import _DEFAULT_EXECUTION_TIMEOUT_SEC, _load_guardrails
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .subprocess_lifecycle import _capture_pgid, _drain_bounded

logger = logging.getLogger(__name__)


def start_tool_execution(tool_id: str, tool: str, input_data: Dict[str, Any]) -> None:
    """Load activity tracking lazily to avoid AgentState's boot import cycle."""
    from .activity_tracking import start_tool_execution as track_start

    track_start(tool_id, tool, input_data)


def complete_tool_execution(
    tool_id: str, success: bool, output: Optional[str] = None
) -> None:
    """Load activity tracking lazily to avoid AgentState's boot import cycle."""
    from .activity_tracking import complete_tool_execution as track_completion

    track_completion(tool_id, success, output)

READ_ONLY_CONFIG = (
    Path(os.getenv("HOME", "/home/developer")) / ".trinity" / "read-only-config.json"
)
DEFAULT_TIMEOUT_SECONDS = 900
MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024
MAX_PROTOCOL_MESSAGES = 10_000
MAX_PROTOCOL_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_STDERR_LINE_BYTES = 64 * 1024
MAX_STDERR_BYTES = 1024 * 1024
SUPPORTED_PROTOCOL_VERSION = 1
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})
_ACP_STOP_FAILURES = {
    "max_tokens": (502, "ACP prompt stopped after reaching the token limit"),
    "max_turn_requests": (422, "ACP prompt exceeded the agent request limit"),
    "refusal": (422, "ACP agent refused the prompt"),
}

_AUTH_PATTERNS = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\bauth[_ ]required\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|incorrect|missing|no)[ _]api[ _]key\b", re.IGNORECASE),
    re.compile(r"\bnot\s+authenticated\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s+(?:failed|error)\b", re.IGNORECASE),
)
_RATE_MARKERS = ("429", "rate limit", "rate_limit", "quota", "too many requests")


def _read_only_enabled() -> bool:
    try:
        value = json.loads(READ_ONLY_CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ACP read-only config is unavailable or malformed; refusing execution: {exc}"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("enabled", False), bool):
        raise RuntimeError(
            "ACP read-only config must be a JSON object with a boolean enabled field"
        )
    return value.get("enabled", False)


def _iso_now() -> str:
    return datetime.now().isoformat()


def _sanitize_protocol_value(value: Any) -> Any:
    """Sanitize an acyclic JSON value without a depth-based leak fallback."""
    if isinstance(value, str):
        return sanitize_text(value)
    if not isinstance(value, (dict, list)):
        return value

    root: Any = {} if isinstance(value, dict) else []
    stack: List[Tuple[Any, Any]] = [(value, root)]
    while stack:
        source, target = stack.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in items:
            clean_key = sanitize_text(key) if isinstance(key, str) else key
            if isinstance(item, str):
                clean_item: Any = sanitize_text(item)
            elif isinstance(item, dict):
                clean_item = {}
                stack.append((item, clean_item))
            elif isinstance(item, list):
                clean_item = []
                stack.append((item, clean_item))
            else:
                clean_item = item
            if isinstance(target, dict):
                target[clean_key] = clean_item
            else:
                target.append(clean_item)
    return root


def _surface_guardrails(kind: str) -> int:
    """Enforce the common wall clock and make unmapped ACP controls visible."""
    guardrails = _load_guardrails()
    disallowed = guardrails.get("disallowed_tools") or []
    if disallowed:
        logger.warning(
            "[ACP] guardrails disallow %s, but generic ACP has no portable per-tool "
            "control; the restriction is surfaced and read-only remains enforced",
            disallowed,
        )
    max_turns = guardrails.get("max_turns_chat" if kind == "chat" else "max_turns_task")
    if max_turns:
        logger.warning(
            "[ACP] guardrail max_turns_%s=%s cannot be mapped portably; enforcing the "
            "wall-clock timeout instead",
            kind,
            max_turns,
        )
    try:
        timeout = int(
            guardrails.get("execution_timeout_sec") or _DEFAULT_EXECUTION_TIMEOUT_SEC
        )
    except (TypeError, ValueError):
        timeout = _DEFAULT_EXECUTION_TIMEOUT_SEC
    return max(1, timeout)


def _map_acp_exception(exc: Exception) -> HTTPException:
    """Map ACP/provider failures onto the status contract consumed by the backend."""
    if isinstance(exc, HTTPException):
        return exc
    detail = sanitize_text(str(exc) or exc.__class__.__name__)[:500]
    lower = detail.lower()
    if any(marker in lower for marker in _RATE_MARKERS):
        return HTTPException(
            status_code=429, detail=f"ACP provider rate limit: {detail}"
        )
    if any(pattern.search(detail) for pattern in _AUTH_PATTERNS):
        return HTTPException(
            status_code=503, detail=f"ACP provider authentication failure: {detail}"
        )
    if "timed out" in lower:
        return HTTPException(status_code=504, detail=detail)
    if (
        "closed stdout" in lower
        or "broken pipe" in lower
        or "connection reset" in lower
    ):
        return HTTPException(status_code=502, detail=detail)
    if "cannot enforce" in lower or "does not support" in lower:
        return HTTPException(status_code=422, detail=detail)
    return HTTPException(status_code=500, detail=f"ACP execution failed: {detail}")


@dataclass
class _PromptState:
    text: List[str] = field(default_factory=list)
    execution_log: List[ExecutionLogEntry] = field(default_factory=list)
    raw_messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_names: Dict[str, str] = field(default_factory=dict)
    completed_tools: set[str] = field(default_factory=set)
    raw_message_bytes: int = 0
    context_used: Optional[int] = None
    context_size: Optional[int] = None
    cumulative_cost_usd: Optional[float] = None
    turn_cost_usd: Optional[float] = None


class _ACPConnection:
    def __init__(
        self,
        manifest: ACPManifest,
        *,
        execution_id: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.manifest = manifest
        self.execution_id = execution_id
        self.execution_tag: Optional[str] = None
        self.model = model
        self.process: Optional[subprocess.Popen[str]] = None
        self.session_id: Optional[str] = None
        self._next_id = 1
        self._io_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._prompt_active = False
        self._prompt_done = threading.Event()
        self._prompt_done.set()
        self._last_stop_reason: Optional[str] = None
        self._last_cost_usd = 0.0

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        read_only = _read_only_enabled()
        if read_only and not self.manifest.read_only_supported:
            raise RuntimeError(
                "ACP harness cannot enforce Trinity read-only mode; refusing execution"
            )
        execution_tag = self.execution_id or f"acp-chat-{uuid.uuid4()}"
        self.execution_tag = execution_tag
        extra = {EXECUTION_TAG_NAME: execution_tag}
        if self.model:
            extra["ACP_MODEL"] = self.model
        if read_only:
            extra["TRINITY_READ_ONLY"] = "1"
        env = build_execution_env(extra=extra)
        self.process = subprocess.Popen(
            list(self.manifest.command),
            cwd=self.manifest.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        initialize_result = self._request(
            "initialize",
            {
                "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                "clientCapabilities": {
                    # Trinity's generic client does not implement ACP reverse
                    # filesystem RPC. Harnesses operate on the mounted workspace
                    # through their own tools, so claiming either method here
                    # would invite a conforming server to call an unavailable API.
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    "auth": {"terminal": False},
                },
                "clientInfo": {"name": "trinity", "version": "1"},
            },
            _PromptState(),
        )
        self._validate_initialize_result(initialize_result)
        result = self._request(
            "session/new",
            {"cwd": self.manifest.cwd, "mcpServers": []},
            _PromptState(),
        )
        self.session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not self.session_id:
            raise RuntimeError("ACP session/new response did not include sessionId")

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        recorded_bytes = 0
        truncation_reported = False
        while True:
            line = process.stderr.readline(MAX_STDERR_LINE_BYTES + 1)
            if not line:
                return
            line_bytes = len(line.encode("utf-8", "replace"))
            if recorded_bytes + line_bytes > MAX_STDERR_BYTES:
                if not truncation_reported:
                    logger.warning(
                        "[ACP stderr] output exceeded %s bytes; further output suppressed",
                        MAX_STDERR_BYTES,
                    )
                    truncation_reported = True
                continue
            recorded_bytes += line_bytes
            clean = sanitize_subprocess_line(line.rstrip())
            if clean:
                logger.info("[ACP stderr] %s", clean)

    @staticmethod
    def _validate_initialize_result(result: Any) -> None:
        if not isinstance(result, dict):
            raise RuntimeError("ACP initialize response must be an object")
        negotiated = result.get("protocolVersion")
        if type(negotiated) is not int or negotiated != SUPPORTED_PROTOCOL_VERSION:
            raise RuntimeError(
                "ACP protocol version mismatch: "
                f"Trinity supports {SUPPORTED_PROTOCOL_VERSION}, agent selected {negotiated!r}"
            )
        auth_methods = result.get("authMethods", [])
        if not isinstance(auth_methods, list):
            raise RuntimeError("ACP initialize authMethods must be an array")
        for index, method in enumerate(auth_methods):
            if not isinstance(method, dict):
                raise RuntimeError(
                    f"ACP initialize authMethods[{index}] must be an object"
                )
            if not isinstance(method.get("id"), str) or not method["id"]:
                raise RuntimeError(
                    f"ACP initialize authMethods[{index}].id must be a string"
                )
            if not isinstance(method.get("name"), str) or not method["name"]:
                raise RuntimeError(
                    f"ACP initialize authMethods[{index}].name must be a string"
                )
            method_type = method.get("type", "agent")
            if method_type == "terminal":
                raise RuntimeError(
                    "ACP agent advertised terminal authentication although "
                    "Trinity set clientCapabilities.auth.terminal=false"
                )
            if method_type != "agent":
                raise RuntimeError(
                    f"ACP initialize authMethods[{index}].type is unsupported"
                )
        capabilities = result.get("agentCapabilities", {})
        if not isinstance(capabilities, dict):
            raise RuntimeError("ACP initialize agentCapabilities must be an object")
        # Advertising login methods does not mean the current process is
        # unauthenticated (Gemini CLI advertises API-key and OAuth choices even
        # when an injected key is already active). Continue with session/new;
        # an actual auth_required response is mapped to the provider-auth error.

    def _write(self, message: Dict[str, Any]) -> None:
        if (
            self.process is None
            or self.process.stdin is None
            or self.process.poll() is not None
        ):
            raise RuntimeError("ACP subprocess is not running")
        with self._write_lock:
            self.process.stdin.write(
                json.dumps(message, separators=(",", ":")) + "\n"
            )
            self.process.stdin.flush()

    def _read(self) -> List[Dict[str, Any]]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("ACP subprocess is not running")
        line = self.process.stdout.readline(MAX_PROTOCOL_LINE_BYTES + 1)
        if not line:
            rc = self.process.poll()
            raise RuntimeError(f"ACP subprocess closed stdout unexpectedly (exit={rc})")
        if len(line.encode("utf-8", "replace")) > MAX_PROTOCOL_LINE_BYTES:
            raise RuntimeError("ACP protocol line exceeded size limit")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ACP stdout contained non-JSON protocol data") from exc
        messages = payload if isinstance(payload, list) else [payload]
        if not messages:
            raise RuntimeError("ACP stdout contained an empty JSON-RPC batch")
        for message in messages:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise RuntimeError("ACP stdout contained an invalid JSON-RPC message")
        return messages

    def _request(self, method: str, params: Dict[str, Any], state: _PromptState) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            response: Optional[Dict[str, Any]] = None
            for message in self._read():
                self._record_raw_message(message, state)
                if message.get("id") == request_id and (
                    "result" in message or "error" in message
                ):
                    response = message
                    continue
                if "method" in message and "id" in message:
                    self._handle_server_request(message, state)
                elif message.get("method") == "session/update":
                    if method != "session/prompt":
                        raise RuntimeError(
                            f"ACP sent session/update while handling {method}"
                        )
                    self._handle_update(message.get("params") or {}, state)
            # Process the whole JSON-RPC batch before returning so notifications
            # adjacent to the response cannot be stranded for the next request.
            if response is not None:
                if "error" in response:
                    raise RuntimeError(
                        f"ACP {method} failed: {sanitize_text(str(response['error']))}"
                    )
                return response.get("result")

    @staticmethod
    def _record_raw_message(message: Dict[str, Any], state: _PromptState) -> None:
        if len(state.raw_messages) >= MAX_PROTOCOL_MESSAGES:
            raise RuntimeError("ACP protocol transcript exceeded message limit")
        clean = _sanitize_protocol_value(message)
        encoded_size = len(
            json.dumps(clean, ensure_ascii=False).encode("utf-8", "replace")
        )
        if state.raw_message_bytes + encoded_size > MAX_PROTOCOL_TRANSCRIPT_BYTES:
            raise RuntimeError("ACP protocol transcript exceeded size limit")
        state.raw_message_bytes += encoded_size
        state.raw_messages.append(clean)

    def _handle_server_request(
        self, message: Dict[str, Any], state: _PromptState
    ) -> None:
        request_id = message.get("id")
        if message.get("method") != "session/request_permission":
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": "method not supported by Trinity ACP client",
                    },
                }
            )
            return
        params = message.get("params") or {}
        options = [
            option
            for option in (params.get("options") or [])
            if isinstance(option, dict)
        ]
        if self.manifest.permission_policy == "allow":
            # Prefer the least durable grant even when the agent presents an
            # allow-always option first.
            allowed = next(
                (option for option in options if option.get("kind") == "allow_once"),
                None,
            )
            if allowed is None:
                allowed = next(
                    (
                        option
                        for option in options
                        if option.get("kind") == "allow_always"
                    ),
                    None,
                )
            outcome = (
                {"outcome": "selected", "optionId": allowed.get("optionId")}
                if allowed
                else {"outcome": "cancelled"}
            )
        else:
            outcome = {"outcome": "cancelled"}
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}}
        )

    def _publish_log(self, entry: ExecutionLogEntry) -> None:
        if self.execution_id:
            get_process_registry().publish_log_entry(
                self.execution_id, entry.model_dump()
            )

    def _handle_update(self, params: Dict[str, Any], state: _PromptState) -> None:
        if not isinstance(params, dict):
            raise RuntimeError("ACP session/update params must be an object")
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or session_id != self.session_id:
            raise RuntimeError("ACP session/update used an unexpected sessionId")
        update = params.get("update") or {}
        if not isinstance(update, dict):
            raise RuntimeError("ACP session/update payload must be an object")
        # This is the earliest boundary shared by persisted logs, live SSE, and
        # activity tracking. Never let an agent-authored protocol field reach
        # one of those sinks before sanitization.
        update = _sanitize_protocol_value(update)
        kind = update.get("sessionUpdate") or update.get("type")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                state.text.append(text)
            return
        if kind == "tool_call":
            tool_id = sanitize_text(
                str(update.get("toolCallId") or update.get("id") or uuid.uuid4())
            )
            name = sanitize_text(
                str(update.get("title") or update.get("kind") or "ACP tool")
            )
            tool_input = update.get("rawInput") or {}
            if not isinstance(tool_input, dict):
                tool_input = {"value": tool_input}
            tool_input = _sanitize_protocol_value(tool_input)
            state.tool_names[tool_id] = name
            status_value = update.get("status")
            entry = ExecutionLogEntry(
                id=tool_id,
                type="tool_use",
                tool=name,
                input=tool_input,
                timestamp=_iso_now(),
            )
            state.execution_log.append(entry)
            self._publish_log(entry)
            try:
                start_tool_execution(tool_id, name, tool_input)
            except Exception:  # activity is best effort
                logger.debug("ACP tool activity start failed", exc_info=True)
            if status_value in _TERMINAL_TOOL_STATUSES:
                self._complete_tool_update(tool_id, update, state)
            return
        if kind == "tool_call_update":
            tool_id = sanitize_text(
                str(update.get("toolCallId") or update.get("id") or "unknown")
            )
            status_value = update.get("status")
            title = update.get("title")
            if isinstance(title, str) and title:
                state.tool_names[tool_id] = sanitize_text(title)
            # ACP tool updates are partial. Pending, in-progress, and status-less
            # frames carry progress only; they must not close Trinity activity.
            if status_value in _TERMINAL_TOOL_STATUSES:
                self._complete_tool_update(tool_id, update, state)
            return
        if kind == "usage_update":
            used = update.get("used")
            size = update.get("size")
            if (
                isinstance(used, bool)
                or not isinstance(used, int)
                or used < 0
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise RuntimeError(
                    "ACP usage_update requires non-negative integer used and size"
                )
            state.context_used = used
            state.context_size = size
            cost = update.get("cost")
            if cost is not None:
                if not isinstance(cost, dict):
                    raise RuntimeError("ACP usage_update cost must be an object")
                amount = cost.get("amount")
                currency = cost.get("currency")
                if (
                    isinstance(amount, bool)
                    or not isinstance(amount, (int, float))
                    or not math.isfinite(amount)
                    or amount < 0
                    or not isinstance(currency, str)
                    or not currency
                ):
                    raise RuntimeError(
                        "ACP usage_update cost requires non-negative amount and currency"
                    )
                if currency.upper() == "USD":
                    state.cumulative_cost_usd = float(amount)

    def _complete_tool_update(
        self,
        tool_id: str,
        update: Dict[str, Any],
        state: _PromptState,
    ) -> None:
        if tool_id in state.completed_tools:
            return
        state.completed_tools.add(tool_id)
        name = state.tool_names.get(tool_id, "ACP tool")
        status_value = update.get("status")
        success = status_value == "completed"
        output = update.get("rawOutput")
        if output is None:
            output = update.get("content")
        if not isinstance(output, str):
            output = (
                json.dumps(output, ensure_ascii=False) if output is not None else ""
            )
        output = sanitize_text(output)
        entry = ExecutionLogEntry(
            id=tool_id,
            type="tool_result",
            tool=name,
            output=output or None,
            success=success,
            timestamp=_iso_now(),
        )
        state.execution_log.append(entry)
        self._publish_log(entry)
        try:
            complete_tool_execution(tool_id, success, output)
        except Exception:
            logger.debug("ACP tool activity completion failed", exc_info=True)

    def _close_pending_tools(self, state: _PromptState, reason: str) -> None:
        """Fail-close activities whose ACP terminal update never arrived."""
        for tool_id in tuple(state.tool_names):
            if tool_id in state.completed_tools:
                continue
            self._complete_tool_update(
                tool_id,
                {"status": "failed", "rawOutput": sanitize_text(reason)},
                state,
            )

    def _update_turn_cost(self, state: _PromptState) -> None:
        """Convert ACP's cumulative session cost to a per-turn delta."""
        if state.cumulative_cost_usd is None:
            return
        cumulative = state.cumulative_cost_usd
        if cumulative >= self._last_cost_usd:
            state.turn_cost_usd = cumulative - self._last_cost_usd
        else:
            logger.warning(
                "[ACP] cumulative session cost decreased from %s to %s; "
                "treating the new value as a reset session total",
                self._last_cost_usd,
                cumulative,
            )
            state.turn_cost_usd = cumulative
        # Advance the baseline even for a refused/cancelled/failed prompt so its
        # spend cannot be misattributed to a later successful turn.
        self._last_cost_usd = cumulative

    @staticmethod
    def _validate_stop_reason(result: Any) -> str:
        if not isinstance(result, dict):
            raise RuntimeError("ACP session/prompt response must be an object")
        stop_reason = result.get("stopReason")
        if not isinstance(stop_reason, str) or not stop_reason:
            raise RuntimeError("ACP session/prompt response is missing stopReason")
        if stop_reason not in {"end_turn", "cancelled", *_ACP_STOP_FAILURES}:
            raise RuntimeError(
                f"ACP session/prompt returned unknown stopReason {stop_reason!r}"
            )
        return stop_reason

    def prompt(self, prompt: str) -> Tuple[str, _PromptState]:
        with self._io_lock:
            self.start()
            state = _PromptState()
            result: Any = None
            pending_reason = "ACP prompt ended before the tool reported completion"
            self._last_stop_reason = None
            self._prompt_done.clear()
            self._prompt_active = True
            try:
                result = self._request(
                    "session/prompt",
                    {
                        "sessionId": self.session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    state,
                )
                stop_reason = self._validate_stop_reason(result)
                self._last_stop_reason = stop_reason
                if stop_reason == "cancelled":
                    pending_reason = "ACP prompt was cancelled before the tool completed"
                    raise HTTPException(
                        status_code=499, detail="ACP prompt was cancelled"
                    )
                failure = _ACP_STOP_FAILURES.get(stop_reason)
                if failure:
                    pending_reason = f"ACP prompt stopped with {stop_reason}"
                    raise HTTPException(status_code=failure[0], detail=failure[1])
            finally:
                self._close_pending_tools(state, pending_reason)
                self._update_turn_cost(state)
                self._prompt_active = False
                self._prompt_done.set()
            response = "".join(state.text)
            if not response and isinstance(result, dict):
                candidate = result.get("text") or result.get("message")
                if isinstance(candidate, str):
                    response = candidate
            return sanitize_text(response), state

    def cancel_prompt(self, wait_timeout: float = 0.0) -> bool:
        if (
            not self._prompt_active
            or not self.session_id
            or self.process is None
            or self.process.poll() is not None
        ):
            return False
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": self.session_id},
                }
            )
        except Exception:
            logger.debug("ACP session/cancel notification failed", exc_info=True)
            return False
        if wait_timeout <= 0:
            return False
        self._prompt_done.wait(wait_timeout)
        return self._prompt_done.is_set() and self._last_stop_reason == "cancelled"

    def close(self) -> None:
        process = self.process
        self.cancel_prompt()
        self.process = None
        self.session_id = None
        if process is None:
            return
        pgid = _capture_pgid(process)
        _drain_bounded(
            process,
            self._stderr_thread,
            grace=2,
            pgid=pgid,
            execution_tag=self.execution_tag,
        )


class ACPRuntime(AgentRuntime):
    def __init__(self, manifest_path: Path = ACP_MANIFEST):
        self.manifest_path = manifest_path
        self._chat: Optional[_ACPConnection] = None
        self._chat_lock = threading.Lock()
        self._active: Dict[str, _ACPConnection] = {}
        self._active_lock = threading.Lock()

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            chat_continuity=True,
            session_tab_resume=False,
            mcp_support=False,
            cost_reporting="unavailable",
        )

    def _manifest(self) -> ACPManifest:
        return _load_manifest(self.manifest_path)

    def is_available(self) -> bool:
        try:
            self._manifest()
            return True
        except RuntimeError:
            return False

    def get_default_model(self) -> str:
        return (
            os.getenv("AGENT_RUNTIME_MODEL")
            or os.getenv("ACP_MODEL")
            or "acp-provider-default"
        )

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        return not bool(mcp_servers)

    def reset_session(self) -> None:
        with self._chat_lock:
            connection, self._chat = self._chat, None
        if connection:
            connection.close()

    def _track_active(
        self, execution_id: Optional[str], connection: _ACPConnection
    ) -> None:
        if execution_id:
            with self._active_lock:
                self._active[execution_id] = connection

    def _untrack_active(
        self, execution_id: Optional[str], connection: _ACPConnection
    ) -> None:
        if execution_id:
            with self._active_lock:
                if self._active.get(execution_id) is connection:
                    del self._active[execution_id]

    def cancel_execution(
        self, execution_id: str, timeout_seconds: float = 2.0
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            with self._active_lock:
                connection = self._active.get(execution_id)
            if connection is None:
                return False
            if connection._prompt_active:
                break
            process = connection.process
            if process is None or process.poll() is not None:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        confirmed = connection.cancel_prompt(
            wait_timeout=max(0.0, deadline - time.monotonic())
        )
        if confirmed:
            get_process_registry().mark_terminated(execution_id)
        return confirmed

    @staticmethod
    def _combined_prompt(prompt: str, system_prompt: Optional[str]) -> str:
        if system_prompt:
            return f"{system_prompt}\n\n{prompt}"
        return prompt

    @staticmethod
    def _metadata(
        *,
        started: float,
        execution_id: Optional[str],
        session_id: Optional[str],
        tool_count: int,
        model: Optional[str],
        state: _PromptState,
    ) -> ExecutionMetadata:
        return ExecutionMetadata(
            cost_usd=state.turn_cost_usd,
            duration_ms=int((time.monotonic() - started) * 1000),
            tool_count=tool_count,
            session_id=session_id,
            execution_id=execution_id,
            status="success",
            model_name=model,
            input_tokens=state.context_used or 0,
            # ACP usage_update is authoritative when present. Otherwise use
            # Trinity's conservative model catalog fallback, just like the
            # other runtimes, rather than encoding "unknown" as a misleading 0.
            context_window=(
                state.context_size
                if state.context_size is not None and state.context_size > 0
                else resolve_context_window(model)
            ),
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
        del stream
        started = time.monotonic()
        effective_model = model or self.get_default_model()
        if not continue_session:
            self.reset_session()
        with self._chat_lock:
            if self._chat is not None and self._chat.model != effective_model:
                connection, self._chat = self._chat, None
                connection.close()
            if self._chat is None:
                self._chat = _ACPConnection(
                    self._manifest(), execution_id=execution_id, model=effective_model
                )
            connection = self._chat
            connection.execution_id = execution_id
        registry = get_process_registry()
        timeout_seconds = _surface_guardrails("chat")
        try:
            # Start before registration so the registry always gets a real handle.
            await asyncio.to_thread(connection.start)
            assert connection.process is not None
            if execution_id:
                registry.register(
                    execution_id,
                    connection.process,
                    {
                        "type": "chat",
                        "runtime": "acp",
                        "pgid": os.getpgid(connection.process.pid),
                    },
                )
            self._track_active(execution_id, connection)
            response, state = await asyncio.wait_for(
                asyncio.to_thread(
                    connection.prompt, self._combined_prompt(prompt, system_prompt)
                ),
                timeout=timeout_seconds,
            )
            metadata = self._metadata(
                started=started,
                execution_id=execution_id,
                session_id=connection.session_id,
                tool_count=sum(
                    1 for item in state.execution_log if item.type == "tool_use"
                ),
                model=effective_model,
                state=state,
            )
            return response, state.execution_log, metadata, state.raw_messages
        except asyncio.TimeoutError as exc:
            connection.close()
            with self._chat_lock:
                if self._chat is connection:
                    self._chat = None
            raise HTTPException(
                status_code=504, detail="ACP execution timed out"
            ) from exc
        except asyncio.CancelledError:
            connection.close()
            with self._chat_lock:
                if self._chat is connection:
                    self._chat = None
            raise
        except Exception as exc:
            # Any request/framing error can leave the byte stream out of sync.
            # Never retain such a connection merely because the child is alive.
            connection.close()
            with self._chat_lock:
                if self._chat is connection:
                    self._chat = None
            raise _map_acp_exception(exc) from exc
        finally:
            self._untrack_active(execution_id, connection)
            if execution_id:
                registry.unregister(execution_id)

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, str]:
        effective_model = model or self.get_default_model()
        if allowed_tools is not None:
            raise HTTPException(
                status_code=422,
                detail="ACP harness cannot enforce allowed_tools; refusing to widen tool scope",
            )
        if max_turns is not None:
            raise HTTPException(
                status_code=422,
                detail="ACP harness cannot enforce max_turns; refusing to discard the limit",
            )
        if resume_session_id or persist_session:
            raise HTTPException(
                status_code=422,
                detail="generic ACP runtime does not support persisted Session-tab resume",
            )
        if images:
            raise HTTPException(
                status_code=422,
                detail="generic ACP runtime does not advertise image input support",
            )
        started = time.monotonic()
        connection = _ACPConnection(
            self._manifest(), execution_id=execution_id, model=effective_model
        )
        registry = get_process_registry()
        guardrail_timeout = _surface_guardrails("task")
        effective_timeout = min(max(1, timeout_seconds), guardrail_timeout)
        try:
            await asyncio.to_thread(connection.start)
            assert connection.process is not None
            if execution_id:
                registry.register(
                    execution_id,
                    connection.process,
                    {
                        "type": "headless",
                        "runtime": "acp",
                        "pgid": os.getpgid(connection.process.pid),
                    },
                )
            self._track_active(execution_id, connection)
            response, state = await asyncio.wait_for(
                asyncio.to_thread(
                    connection.prompt, self._combined_prompt(prompt, system_prompt)
                ),
                timeout=effective_timeout,
            )
            session_id = connection.session_id or ""
            metadata = self._metadata(
                started=started,
                execution_id=execution_id,
                session_id=session_id,
                tool_count=sum(
                    1 for item in state.execution_log if item.type == "tool_use"
                ),
                model=effective_model,
                state=state,
            )
            return response, state.execution_log, metadata, session_id
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="ACP headless execution timed out"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _map_acp_exception(exc) from exc
        finally:
            self._untrack_active(execution_id, connection)
            if execution_id:
                registry.unregister(execution_id)
            connection.close()


_acp_runtime: Optional[ACPRuntime] = None


def get_acp_runtime() -> ACPRuntime:
    global _acp_runtime
    if _acp_runtime is None:
        _acp_runtime = ACPRuntime()
    return _acp_runtime
