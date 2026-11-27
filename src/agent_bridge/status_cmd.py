"""`agent-bridge status`: what the bridge is running now. Placeholder until the command is built."""

from __future__ import annotations

import sys


def run(argv: list[str]) -> int:
    print("agent-bridge status: not built yet", file=sys.stderr)
    return 2
