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
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.credential_sanitizer import (
    sanitize_dict,
    sanitize_subprocess_line,
    sanitize_text,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import _DEFAULT_EXECUTION_TIMEOUT_SEC, _load_guardrails
from .activity_tracking import complete_tool_execution, start_tool_execution
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .subprocess_lifecycle import _capture_pgid, _drain_bounded

logger = logging.getLogger(__name__)

ACP_MANIFEST = Path(os.getenv("TRINITY_ACP_MANIFEST", "/opt/trinity/acp/runtime.json"))
READ_ONLY_CONFIG = (
    Path(os.getenv("HOME", "/home/developer")) / ".trinity" / "read-only-config.json"
)
DEFAULT_TIMEOUT_SECONDS = 900
MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PROTOCOL_MESSAGES = 10_000
MAX_PROTOCOL_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_STDERR_LINE_BYTES = 64 * 1024
MAX_STDERR_BYTES = 1024 * 1024
SUPPORTED_PROTOCOL_VERSION = 1
_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed"})

_AUTH_PATTERNS = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\bauth[_ ]required\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|incorrect|missing|no)[ _]api[ _]key\b", re.IGNORECASE),
    re.compile(r"\bnot\s+authenticated\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s+(?:failed|error)\b", re.IGNORECASE),
)
_RATE_MARKERS = ("429", "rate limit", "rate_limit", "quota", "too many requests")


@dataclass(frozen=True)
class ACPManifest:
    command: Tuple[str, ...]
    cwd: str = "/workspace"
    permission_policy: str = "reject"
    read_only_supported: bool = False


def _validate_trusted_parent_chain(path: Path) -> None:
    """Reject a trusted file whose pathname can be replaced by the agent user."""
    current = path.parent
    while True:
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"ACP trusted parent is unavailable: {current}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"ACP trusted parent is not a directory: {current}")
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(
                f"ACP trusted parent must be root-owned and not group/world writable: {current}"
            )
        if current.parent == current:
            return
        current = current.parent


def _validate_trusted_stat(
    path: Path, info: os.stat_result, *, executable: bool
) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"ACP trusted file is not a regular file: {path}")
    if info.st_uid != 0:
        raise RuntimeError(f"ACP trusted file must be owned by root: {path}")
    if info.st_mode & 0o022:
        raise RuntimeError(f"ACP trusted file must not be group/world writable: {path}")
    if executable and not info.st_mode & 0o111:
        raise RuntimeError(f"ACP launcher is not executable: {path}")


def _trusted_file(path: Path, *, executable: bool = False) -> os.stat_result:
    """Open without following symlinks and enforce the derived-image trust boundary."""
    _validate_trusted_parent_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"ACP trusted file is unavailable: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    _validate_trusted_stat(path, info, executable=executable)
    return info


def _load_manifest(path: Path = ACP_MANIFEST) -> ACPManifest:
    _validate_trusted_parent_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            _validate_trusted_stat(path, os.fstat(fd), executable=False)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                text = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(fd)
        if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise RuntimeError(
                f"ACP runtime manifest exceeds {MAX_MANIFEST_BYTES} bytes"
            )
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid ACP runtime manifest {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("ACP runtime manifest must be a JSON object")
    if raw.get("version") != 1:
        raise RuntimeError("ACP runtime manifest version must be 1")
    command = raw.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(v, str) and v for v in command)
    ):
        raise RuntimeError(
            "ACP runtime manifest command must be a non-empty string array"
        )
    launcher = Path(command[0])
    if not launcher.is_absolute():
        raise RuntimeError("ACP launcher path must be absolute")
    _trusted_file(launcher, executable=True)
    cwd = raw.get("cwd", "/workspace")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise RuntimeError("ACP runtime manifest cwd must be absolute")
    policy = raw.get("permission_policy", "reject")
    if policy not in {"allow", "reject"}:
        raise RuntimeError("ACP permission_policy must be 'allow' or 'reject'")
    read_only = raw.get("read_only") or {}
    if not isinstance(read_only, dict) or not isinstance(
        read_only.get("supported", False), bool
    ):
        raise RuntimeError("ACP read_only must contain a boolean supported field")
    return ACPManifest(
        tuple(command), cwd, policy, bool(read_only.get("supported", False))
    )


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
        self._stderr_thread: Optional[threading.Thread] = None
        self._prompt_active = False

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
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read(self) -> Dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("ACP subprocess is not running")
        line = self.process.stdout.readline(MAX_PROTOCOL_LINE_BYTES + 1)
        if not line:
            rc = self.process.poll()
            raise RuntimeError(f"ACP subprocess closed stdout unexpectedly (exit={rc})")
        if len(line.encode("utf-8", "replace")) > MAX_PROTOCOL_LINE_BYTES:
            raise RuntimeError("ACP protocol line exceeded size limit")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ACP stdout contained non-JSON protocol data") from exc
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise RuntimeError("ACP stdout contained an invalid JSON-RPC message")
        return message

    def _request(self, method: str, params: Dict[str, Any], state: _PromptState) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        while True:
            message = self._read()
            self._record_raw_message(message, state)
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                if "error" in message:
                    raise RuntimeError(
                        f"ACP {method} failed: {sanitize_text(str(message['error']))}"
                    )
                return message.get("result")
            if "method" in message and "id" in message:
                self._handle_server_request(message, state)
            elif message.get("method") == "session/update":
                self._handle_update(message.get("params") or {}, state)

    @staticmethod
    def _record_raw_message(message: Dict[str, Any], state: _PromptState) -> None:
        if len(state.raw_messages) >= MAX_PROTOCOL_MESSAGES:
            raise RuntimeError("ACP protocol transcript exceeded message limit")
        clean = sanitize_dict(message)
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
        update = params.get("update") or {}
        kind = update.get("sessionUpdate") or update.get("type")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                state.text.append(text)
            return
        if kind == "tool_call":
            tool_id = str(update.get("toolCallId") or update.get("id") or uuid.uuid4())
            name = str(update.get("title") or update.get("kind") or "ACP tool")
            tool_input = update.get("rawInput") or {}
            if not isinstance(tool_input, dict):
                tool_input = {"value": tool_input}
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
            tool_id = str(update.get("toolCallId") or update.get("id") or "unknown")
            status_value = update.get("status")
            title = update.get("title")
            if isinstance(title, str) and title:
                state.tool_names[tool_id] = title
            # ACP tool updates are partial. Pending, in-progress, and status-less
            # frames carry progress only; they must not close Trinity activity.
            if status_value in _TERMINAL_TOOL_STATUSES:
                self._complete_tool_update(tool_id, update, state)

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

    def prompt(self, prompt: str) -> Tuple[str, _PromptState]:
        with self._io_lock:
            self.start()
            state = _PromptState()
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
            finally:
                self._prompt_active = False
            if isinstance(result, dict):
                stop_reason = result.get("stopReason")
                if stop_reason == "cancelled":
                    raise HTTPException(
                        status_code=499, detail="ACP prompt was cancelled"
                    )
                if stop_reason not in {None, "end_turn"}:
                    logger.warning("[ACP] prompt stopped with reason %r", stop_reason)
            response = "".join(state.text)
            if not response and isinstance(result, dict):
                candidate = result.get("text") or result.get("message")
                if isinstance(candidate, str):
                    response = candidate
            return sanitize_text(response), state

    def cancel_prompt(self) -> None:
        if (
            not self._prompt_active
            or not self.session_id
            or self.process is None
            or self.process.poll() is not None
        ):
            return
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
    ) -> ExecutionMetadata:
        return ExecutionMetadata(
            cost_usd=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            tool_count=tool_count,
            session_id=session_id,
            execution_id=execution_id,
            status="success",
            model_name=model,
            # Generic ACP currently receives no portable token/context
            # telemetry. Zero means unknown and avoids presenting the model
            # class's legacy 200K default as measured provider truth.
            context_window=0,
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
            if execution_id:
                registry.unregister(execution_id)
            connection.close()


_acp_runtime: Optional[ACPRuntime] = None


def get_acp_runtime() -> ACPRuntime:
    global _acp_runtime
    if _acp_runtime is None:
        _acp_runtime = ACPRuntime()
    return _acp_runtime
