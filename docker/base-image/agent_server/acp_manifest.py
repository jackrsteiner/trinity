"""Trusted manifest boundary for the generic ACP runtime.

This module intentionally has no dependency on ``agent_server.services`` so
``AgentState`` can perform its boot availability check without importing the
runtime package while the global state singleton is still being constructed.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


ACP_MANIFEST = Path(os.getenv("TRINITY_ACP_MANIFEST", "/opt/trinity/acp/runtime.json"))
MAX_MANIFEST_BYTES = 64 * 1024


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
                "ACP trusted parent must be root-owned and not group/world "
                f"writable: {current}"
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


def trusted_file(path: Path, *, executable: bool = False) -> os.stat_result:
    """Open without following symlinks and enforce the image trust boundary."""
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


def load_acp_manifest(path: Path = ACP_MANIFEST) -> ACPManifest:
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
        or not all(isinstance(value, str) and value for value in command)
    ):
        raise RuntimeError(
            "ACP runtime manifest command must be a non-empty string array"
        )
    launcher = Path(command[0])
    if not launcher.is_absolute():
        raise RuntimeError("ACP launcher path must be absolute")
    trusted_file(launcher, executable=True)
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
