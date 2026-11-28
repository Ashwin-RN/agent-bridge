"""Command line for `agent-bridge` and `python -m agent_bridge`.

With no argument, or with `serve`, it starts the stdio MCP server, exactly as every existing
registration expects. The other commands never start the server and never open the job manager:

- `status`   what the bridge is running now, read from the database without writing to it
- `install`  write this bridge into a host CLI's configuration
- `doctor`   check the backends, the registrations and their timeouts

Each command lives in its own module and is imported only when it is asked for, so `serve` pays
nothing for them and a fault in one cannot stop the server from starting.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

USAGE = """\
usage: agent-bridge [command] [options]

  (no command)   start the stdio MCP server
  serve          the same
  status         what the bridge is running now (--line, --watch, --json)
  install        register the bridge in a host CLI (--host NAME, --print)
  doctor         check backends, registrations and timeouts
  --version      print the version

Run `agent-bridge <command> --help` for a command's own options.
"""


def _serve(_argv: Sequence[str]) -> int:
    from agent_bridge.server import main as serve_main

    serve_main()
    return 0


def _status(argv: Sequence[str]) -> int:
    from agent_bridge.status_cmd import run

    return run(list(argv))


def _install(argv: Sequence[str]) -> int:
    from agent_bridge.install_cmd import run_install

    return run_install(list(argv))


def _doctor(argv: Sequence[str]) -> int:
    from agent_bridge.install_cmd import run_doctor

    return run_doctor(list(argv))


COMMANDS: dict[str, Callable[[Sequence[str]], int]] = {
    "serve": _serve,
    "status": _status,
    "install": _install,
    "doctor": _doctor,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and return its exit code. No argument means `serve`."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _serve(args)
    command, rest = args[0], args[1:]
    if command in {"-h", "--help", "help"}:
        print(USAGE, end="")
        return 0
    if command in {"-V", "--version"}:
        from agent_bridge import __version__

        print(f"agent-bridge {__version__}")
        return 0
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"agent-bridge: unknown command {command!r}\n\n{USAGE}", end="", file=sys.stderr)
        return 2
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
