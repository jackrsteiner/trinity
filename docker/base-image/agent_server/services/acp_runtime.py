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
import signal
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..utils.credential_sanitizer import sanitize_dict, sanitize_subprocess_line, sanitize_text
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from .activity_tracking import complete_tool_execution, start_tool_execution
from .execution_env import build_execution_env
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities

logger = logging.getLogger(__name__)

ACP_MANIFEST = Path(os.getenv("TRINITY_ACP_MANIFEST", "/opt/trinity/acp/runtime.json"))
READ_ONLY_CONFIG = Path(os.getenv("HOME", "/home/developer")) / ".trinity" / "read-only-config.json"
DEFAULT_TIMEOUT_SECONDS = 900
MAX_PROTOCOL_LINE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ACPManifest:
    command: Tuple[str, ...]
    cwd: str = "/workspace"
    permission_policy: str = "reject"
    read_only_supported: bool = False


def _trusted_file(path: Path, *, executable: bool = False) -> os.stat_result:
    """Open without following symlinks and enforce the derived-image trust boundary."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"ACP trusted file is unavailable: {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"ACP trusted file is not a regular file: {path}")
    if info.st_uid != 0:
        raise RuntimeError(f"ACP trusted file must be owned by root: {path}")
    if info.st_mode & 0o022:
        raise RuntimeError(f"ACP trusted file must not be group/world writable: {path}")
    if executable and not info.st_mode & 0o111:
        raise RuntimeError(f"ACP launcher is not executable: {path}")
    return info


def _load_manifest(path: Path = ACP_MANIFEST) -> ACPManifest:
    _trusted_file(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid ACP runtime manifest {path}: {exc}") from exc
    if raw.get("version") != 1:
        raise RuntimeError("ACP runtime manifest version must be 1")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command):
        raise RuntimeError("ACP runtime manifest command must be a non-empty string array")
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
    if not isinstance(read_only, dict) or not isinstance(read_only.get("supported", False), bool):
        raise RuntimeError("ACP read_only must contain a boolean supported field")
    return ACPManifest(tuple(command), cwd, policy, bool(read_only.get("supported", False)))


def _read_only_enabled() -> bool:
    try:
        value = json.loads(READ_ONLY_CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ACP read-only config unavailable or malformed: %s", exc)
        return False
    return bool(value.get("enabled"))


def _iso_now() -> str:
    return datetime.now().isoformat()


@dataclass
class _PromptState:
    text: List[str] = field(default_factory=list)
    execution_log: List[ExecutionLogEntry] = field(default_factory=list)
    raw_messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_names: Dict[str, str] = field(default_factory=dict)


class _ACPConnection:
    def __init__(self, manifest: ACPManifest, *, execution_id: Optional[str] = None):
        self.manifest = manifest
        self.execution_id = execution_id
        self.process: Optional[subprocess.Popen[str]] = None
        self.session_id: Optional[str] = None
        self._next_id = 1
        self._io_lock = threading.Lock()
        self._stderr_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        read_only = _read_only_enabled()
        if read_only and not self.manifest.read_only_supported:
            raise RuntimeError("ACP harness cannot enforce Trinity read-only mode; refusing execution")
        extra = {EXECUTION_TAG_NAME: self.execution_id or f"acp-chat-{uuid.uuid4()}"}
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
        self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
                "clientInfo": {"name": "trinity", "version": "1"},
            },
            _PromptState(),
        )
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
        for line in process.stderr:
            clean = sanitize_subprocess_line(line.rstrip())
            if clean:
                logger.info("[ACP stderr] %s", clean)

    def _write(self, message: Dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
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
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            message = self._read()
            state.raw_messages.append(sanitize_dict(message))
            if message.get("id") == request_id and ("result" in message or "error" in message):
                if "error" in message:
                    raise RuntimeError(f"ACP {method} failed: {sanitize_text(str(message['error']))}")
                return message.get("result")
            if "method" in message and "id" in message:
                self._handle_server_request(message, state)
            elif message.get("method") == "session/update":
                self._handle_update(message.get("params") or {}, state)

    def _handle_server_request(self, message: Dict[str, Any], state: _PromptState) -> None:
        request_id = message.get("id")
        if message.get("method") != "session/request_permission":
            self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not supported by Trinity ACP client"},
            })
            return
        params = message.get("params") or {}
        options = params.get("options") or []
        if self.manifest.permission_policy == "allow":
            allowed = next(
                (option for option in options if option.get("kind") in {"allow_once", "allow_always"}),
                None,
            )
            outcome = (
                {"outcome": "selected", "optionId": allowed.get("optionId")}
                if allowed else {"outcome": "cancelled"}
            )
        else:
            outcome = {"outcome": "cancelled"}
        self._write({"jsonrpc": "2.0", "id": request_id, "result": {"outcome": outcome}})

    def _publish_log(self, entry: ExecutionLogEntry) -> None:
        if self.execution_id:
            get_process_registry().publish_log_entry(self.execution_id, entry.model_dump())

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
            entry = ExecutionLogEntry(
                id=tool_id, type="tool_use", tool=name, input=tool_input, timestamp=_iso_now()
            )
            state.execution_log.append(entry)
            self._publish_log(entry)
            try:
                start_tool_execution(tool_id, name, tool_input)
            except Exception:  # activity is best effort
                logger.debug("ACP tool activity start failed", exc_info=True)
            return
        if kind == "tool_call_update":
            tool_id = str(update.get("toolCallId") or update.get("id") or "unknown")
            name = state.tool_names.get(tool_id, "ACP tool")
            status_value = update.get("status")
            success = status_value not in {"failed", "cancelled"}
            output = update.get("rawOutput")
            if output is None:
                output = update.get("content")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False) if output is not None else ""
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
            result = self._request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
                state,
            )
            response = "".join(state.text)
            if not response and isinstance(result, dict):
                candidate = result.get("text") or result.get("message")
                if isinstance(candidate, str):
                    response = candidate
            return sanitize_text(response), state

    def close(self) -> None:
        process = self.process
        self.process = None
        self.session_id = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


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
            cost_reporting="estimated",
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
        return os.getenv("ACP_MODEL", "acp-provider-default")

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
        *, started: float, execution_id: Optional[str], session_id: Optional[str], tool_count: int
    ) -> ExecutionMetadata:
        return ExecutionMetadata(
            cost_usd=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            tool_count=tool_count,
            session_id=session_id,
            execution_id=execution_id,
            status="success",
            model_name=os.getenv("ACP_MODEL") or None,
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
        del model, stream
        started = time.monotonic()
        if not continue_session:
            self.reset_session()
        with self._chat_lock:
            if self._chat is None:
                self._chat = _ACPConnection(self._manifest(), execution_id=execution_id)
            connection = self._chat
            connection.execution_id = execution_id
        registry = get_process_registry()
        try:
            # Start before registration so the registry always gets a real handle.
            await asyncio.to_thread(connection.start)
            assert connection.process is not None
            if execution_id:
                registry.register(
                    execution_id,
                    connection.process,
                    {"type": "chat", "runtime": "acp", "pgid": os.getpgid(connection.process.pid)},
                )
            response, state = await asyncio.wait_for(
                asyncio.to_thread(connection.prompt, self._combined_prompt(prompt, system_prompt)),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            metadata = self._metadata(
                started=started,
                execution_id=execution_id,
                session_id=connection.session_id,
                tool_count=sum(1 for item in state.execution_log if item.type == "tool_use"),
            )
            return response, state.execution_log, metadata, state.raw_messages
        except asyncio.TimeoutError as exc:
            connection.close()
            with self._chat_lock:
                if self._chat is connection:
                    self._chat = None
            raise RuntimeError("ACP execution timed out") from exc
        except BaseException:
            if connection.process is None or connection.process.poll() is not None:
                with self._chat_lock:
                    if self._chat is connection:
                        self._chat = None
            raise
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
        del model
        if allowed_tools is not None:
            raise RuntimeError("ACP harness cannot enforce allowed_tools; refusing to widen tool scope")
        if max_turns is not None:
            raise RuntimeError("ACP harness cannot enforce max_turns; refusing to discard the limit")
        if resume_session_id or persist_session:
            raise RuntimeError("generic ACP runtime does not support persisted Session-tab resume")
        if images:
            raise RuntimeError("generic ACP runtime does not advertise image input support")
        started = time.monotonic()
        connection = _ACPConnection(self._manifest(), execution_id=execution_id)
        registry = get_process_registry()
        try:
            await asyncio.to_thread(connection.start)
            assert connection.process is not None
            if execution_id:
                registry.register(
                    execution_id,
                    connection.process,
                    {"type": "headless", "runtime": "acp", "pgid": os.getpgid(connection.process.pid)},
                )
            response, state = await asyncio.wait_for(
                asyncio.to_thread(connection.prompt, self._combined_prompt(prompt, system_prompt)),
                timeout=timeout_seconds,
            )
            session_id = connection.session_id or ""
            metadata = self._metadata(
                started=started,
                execution_id=execution_id,
                session_id=session_id,
                tool_count=sum(1 for item in state.execution_log if item.type == "tool_use"),
            )
            return response, state.execution_log, metadata, session_id
        except asyncio.TimeoutError as exc:
            raise RuntimeError("ACP headless execution timed out") from exc
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
