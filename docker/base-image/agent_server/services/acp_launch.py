"""Generic ACP agent process launch configuration.

This module is deliberately separate from :mod:`acp_runtime`: environment and
template transport are Trinity integration concerns, not ACP protocol behavior.
The runtime receives an immutable executable/argument vector and never branches
on its contents.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Mapping, Optional


class ACPLaunchConfigError(ValueError):
    """The generic ACP process launch configuration is absent or malformed."""


@dataclass(frozen=True)
class ACPLaunchConfig:
    """Shell-free command vector used to start a conforming ACP agent."""

    command: str
    args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.command or not self.command.strip():
            raise ACPLaunchConfigError("ACP runtime requires a non-empty command")
        if "\x00" in self.command:
            raise ACPLaunchConfigError("ACP command contains a NUL byte")
        if len(self.args) > 128:
            raise ACPLaunchConfigError("ACP runtime accepts at most 128 arguments")
        for arg in self.args:
            if not isinstance(arg, str):
                raise ACPLaunchConfigError("ACP runtime arguments must be strings")
            if "\x00" in arg:
                raise ACPLaunchConfigError("ACP runtime argument contains a NUL byte")

    def is_available(self) -> bool:
        """Whether the configured executable can be resolved without running it."""
        if os.path.sep in self.command:
            return os.path.isfile(self.command) and os.access(self.command, os.X_OK)
        return shutil.which(self.command) is not None


def load_acp_launch_config(env: Optional[Mapping[str, str]] = None) -> ACPLaunchConfig:
    """Read Trinity's generic ACP launch envelope.

    ``AGENT_RUNTIME_COMMAND`` is one executable and ``AGENT_RUNTIME_ARGS`` is a
    JSON string array. The names describe runtime process configuration rather
    than any agent or provider. No shell parsing is performed.
    """
    source = os.environ if env is None else env
    command = source.get("AGENT_RUNTIME_COMMAND", "")
    raw_args = source.get("AGENT_RUNTIME_ARGS", "[]") or "[]"
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError as exc:
        raise ACPLaunchConfigError("AGENT_RUNTIME_ARGS must be a JSON string array") from exc
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ACPLaunchConfigError("AGENT_RUNTIME_ARGS must be a JSON string array")
    return ACPLaunchConfig(command=command, args=tuple(args))
