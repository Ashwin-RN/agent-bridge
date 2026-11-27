"""`agent-bridge install` and `agent-bridge doctor`. Placeholders until the commands are built."""

from __future__ import annotations

import sys


def run_install(argv: list[str]) -> int:
    print("agent-bridge install: not built yet", file=sys.stderr)
    return 2


def run_doctor(argv: list[str]) -> int:
    print("agent-bridge doctor: not built yet", file=sys.stderr)
    return 2
