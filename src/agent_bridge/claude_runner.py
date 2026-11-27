"""Compatibility re-exports for the Claude Code runner (implementation in `backends/claude.py`)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from agent_bridge.backends.claude import (
    CLAUDE_API_CREDENTIAL_ENV,
    CLAUDE_EDIT_DENY_RULES,
    CLAUDE_EDIT_PERMISSION_MODE,
    CLAUDE_MIN_VERSION,
    CLAUDE_READ_ONLY_PERMISSION_MODE,
    CLAUDE_READ_ONLY_REMOVED_TOOLS,
    ClaudeRunner,
    _describe_claude_event,
    _sum_model_usage,
    _without_shell_shim,
    parse_claude_stream_events,
    parse_claude_version,
    resolve_claude_command,
)

__all__ = [
    "CLAUDE_API_CREDENTIAL_ENV",
    "CLAUDE_EDIT_DENY_RULES",
    "CLAUDE_EDIT_PERMISSION_MODE",
    "CLAUDE_MIN_VERSION",
    "CLAUDE_READ_ONLY_PERMISSION_MODE",
    "CLAUDE_READ_ONLY_REMOVED_TOOLS",
    "ClaudeRunner",
    "Path",
    "parse_claude_stream_events",
    "parse_claude_version",
    "resolve_claude_command",
    "shutil",
    "sys",
]
