"""Detect a bridge that was started inside one of the bridge's own jobs.

Once the bridge is registered in more than one CLI, a subagent can see the bridge's tools too:
host -> bridge -> codex -> bridge -> claude -> ... A nested bridge must refuse to start jobs.

Two signals are used, because neither is enough alone:

- A marker variable set in every subagent's environment. Cheap, but a CLI may not pass its
  environment on to the MCP servers it starts (Codex passes only a fixed list).
- The process tree. An ancestor can be identified by a live owner row or its server command line,
  even when it uses a separate state directory.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_PARENT_JOB = "AGENT_BRIDGE_PARENT_JOB"
ENV_PARENT_OWNER = "AGENT_BRIDGE_PARENT_OWNER"

# What Claude Code sets for its own running session. A `claude -p` child must not inherit them,
# or it would take itself for a part of the parent session.
CLAUDE_SESSION_ENV: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_CHILD_NOTES_DIR",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_AUTO_CONNECT_IDE",
    "CLAUDE_CODE_SSE_PORT",
)

# A process may register as an owner a moment after it starts, and clocks are read at two places.
_PID_REUSE_SLACK_SECONDS = 2.0
_MAX_ANCESTORS = 64

NESTED_ERROR_CODE = "nested_bridge"


class NestedBridgeError(RuntimeError):
    """Raised when a nested bridge is asked to start a job."""


@dataclass(frozen=True, slots=True)
class Nesting:
    nested: bool | None
    via: str | None = None  # "env" or "ancestry"
    parent_job: str | None = None
    parent_owner: str | None = None
    parent_pid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"nested": self.nested is True}
        if self.nested:
            out["via"] = self.via
            if self.parent_job:
                out["parent_job"] = self.parent_job
            if self.parent_owner:
                out["parent_owner"] = self.parent_owner
            if self.parent_pid is not None:
                out["parent_pid"] = self.parent_pid
        return out

    def refusal(self, tool: str) -> str:
        where = (
            f"job {self.parent_job}"
            if self.parent_job
            else (f"bridge process {self.parent_pid}" if self.parent_pid is not None else "another bridge job")
        )
        return (
            f"{NESTED_ERROR_CODE}: this agent-bridge was started inside {where} (detected by {self.via}). "
            f"A subagent may not start further bridge jobs, so `{tool}` is refused. "
            "Do the work yourself, or report back to the session that started you. "
            "check_jobs, get_usage and list_backends still work."
        )


NOT_NESTED = Nesting(nested=False)
UNKNOWN_NESTING = Nesting(nested=None)


def subagent_env(
    *,
    job_id: str | None,
    owner_id: str | None,
    base: Mapping[str, str] | None = None,
    drop: Iterable[str] = (),
) -> dict[str, str]:
    """The environment for a subagent process: the bridge's own, plus the nesting marker."""
    env = dict(os.environ if base is None else base)
    for name in drop:
        env.pop(name, None)
    env[ENV_PARENT_JOB] = job_id or "unknown"
    if owner_id:
        env[ENV_PARENT_OWNER] = owner_id
    return env


def _windows_process_table() -> dict[int, int]:
    """Map every pid to its parent pid from one Toolhelp snapshot."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snap is None or snap == wintypes.HANDLE(-1).value:
        return {}
    table: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _windows_start_time(pid: int) -> float | None:
    """Wall-clock creation time of a process, or None when it cannot be opened."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        if ticks <= 0:
            return None
        return (ticks - 116_444_736_000_000_000) / 10_000_000.0
    finally:
        k32.CloseHandle(handle)


def _windows_command_line(pid: int) -> list[str] | None:
    import ctypes
    from ctypes import wintypes

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
    query.argtypes = [
        wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
    ]
    query.restype = wintypes.LONG
    split = ctypes.WinDLL("shell32").CommandLineToArgvW
    split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(wintypes.LPWSTR)

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        needed = wintypes.ULONG()
        info_class = 60  # ProcessCommandLineInformation
        query(handle, info_class, None, 0, ctypes.byref(needed))
        if needed.value < ctypes.sizeof(UNICODE_STRING):
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if query(handle, info_class, buffer, len(buffer), ctypes.byref(needed)) != 0:
            return None
        command = UNICODE_STRING.from_buffer(buffer)
        if not command.Length:
            return []
        line = ctypes.wstring_at(command.Buffer, command.Length // ctypes.sizeof(ctypes.c_wchar))
    finally:
        kernel32.CloseHandle(handle)

    argc = ctypes.c_int()
    argv = split(line, ctypes.byref(argc))
    if not argv:
        return None
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


def _is_own_windows_python_launcher(pid: int, argv: list[str]) -> bool:
    # The venv redirector changes argv[0] to the base interpreter and forwards the arguments.
    if sys.platform != "win32" or pid != os.getppid() or sys.executable == sys._base_executable:
        return False
    if argv[1:] != sys.orig_argv[1:]:
        return False

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    image_name = kernel32.QueryFullProcessImageNameW
    image_name.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    image_name.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        path = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(path))
        if not image_name(handle, 0, path, ctypes.byref(size)):
            return False
        return os.path.normcase(path.value) == os.path.normcase(sys.executable)
    finally:
        kernel32.CloseHandle(handle)


def _process_command_line(pid: int) -> list[str] | None:
    try:
        if sys.platform == "win32":
            return _windows_command_line(pid)
        if os.path.isdir("/proc/self"):
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0")
            return [os.fsdecode(arg) for arg in raw.split(b"\0")] if raw else []
        out = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return shlex.split(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _is_bridge_command(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = re.split(r"[/\\]", argv[0])[-1].lower()
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?", executable):
        return False
    # Scan Python's options only, so a script, -c program or another module cannot
    # be mistaken for the bridge merely because its arguments mention "-m agent_bridge".
    args = iter(argv[1:])
    for arg in args:
        if arg == "-m":
            return next(args, None) == "agent_bridge"
        if arg.startswith("-m"):
            return arg[2:] == "agent_bridge"
        if arg in {"-W", "-X"}:
            next(args, None)
        elif not arg.startswith("-") or arg.startswith("-c") or arg in {"-", "--"}:
            break
    return False


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Return `(ppid, starttime_ticks)` from the text of `/proc/<pid>/stat`."""
    # The command name sits in brackets and may itself hold spaces or brackets: split after the last one.
    _, sep, rest = text.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    # After the name: state(0) ppid(1) ... starttime is field 22 overall, so index 19 here.
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def parse_etime(text: str) -> float | None:
    """Seconds from a `ps` elapsed time: `[[dd-]hh:]mm:ss`."""
    days = 0
    rest = text.strip()
    if "-" in rest:
        day_part, _, rest = rest.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = rest.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(days * 86_400 + seconds)


def parse_ps_table(text: str) -> dict[int, tuple[int, float | None]]:
    """Map pid to `(parent pid, seconds since it started)` from `ps -axo pid=,ppid=,etime=`.

    The elapsed time is None where the column is missing or cannot be read.
    """
    table: dict[int, tuple[int, float | None]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parse_etime(parts[2]) if len(parts) > 2 else None)
    return table


def _linux_boot_time() -> float | None:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _posix_ancestors(pid: int) -> list[tuple[int, float | None]] | None:
    chain: list[tuple[int, float | None]] = []
    if os.path.isdir("/proc/self"):
        boot = _linux_boot_time()
        try:
            ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError):
            ticks_per_second = 100.0
        current = pid
        seen = {pid}
        while len(chain) < _MAX_ANCESTORS:
            try:
                with open(f"/proc/{current}/stat", encoding="utf-8", errors="replace") as fh:
                    parsed = parse_proc_stat(fh.read())
            except OSError:
                return chain or None
            if parsed is None:
                return chain or None
            parent = parsed[0]
            if parent <= 0 or parent in seen:
                break
            start: float | None = None
            try:
                with open(f"/proc/{parent}/stat", encoding="utf-8", errors="replace") as fh:
                    parent_parsed = parse_proc_stat(fh.read())
                if parent_parsed is not None and boot is not None:
                    start = boot + parent_parsed[1] / ticks_per_second
            except OSError:
                pass
            chain.append((parent, start))
            seen.add(parent)
            current = parent
        return chain

    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table = parse_ps_table(out)
    if pid not in table:
        return None
    now = time.time()
    current = pid
    seen = {pid}
    while len(chain) < _MAX_ANCESTORS:
        parent = table.get(current, (0, None))[0]
        if parent <= 0 or parent in seen:
            break
        elapsed = table.get(parent, (0, None))[1]
        chain.append((parent, now - elapsed if elapsed is not None else None))
        seen.add(parent)
        current = parent
    return chain


def ancestor_chain(pid: int | None = None) -> list[tuple[int, float | None]] | None:
    """`(pid, start_time)` for each ancestor of `pid`, nearest first. None when unavailable.

    `start_time` is wall-clock seconds, or None where it cannot be read.
    """
    own = os.getpid() if pid is None else int(pid)
    try:
        if sys.platform != "win32":
            return _posix_ancestors(own)

        table = _windows_process_table()
        if own not in table:
            return None
        chain: list[tuple[int, float | None]] = []
        seen = {own}
        current = own
        current_start = _windows_start_time(own)
        while len(chain) < _MAX_ANCESTORS:
            parent = table.get(current, 0)
            if parent <= 0 or parent in seen or parent not in table:
                break
            parent_start = _windows_start_time(parent)
            # Windows keeps the parent's pid after the parent has gone, and pids are reused. A "parent"
            # that started after its child is some other process that took over the number.
            if (
                parent_start is not None
                and current_start is not None
                and parent_start > current_start + _PID_REUSE_SLACK_SECONDS
            ):
                break
            chain.append((parent, parent_start))
            seen.add(parent)
            current = parent
            current_start = parent_start
        return chain
    except Exception:
        return None


def detect_nesting(
    *,
    environ: Mapping[str, str] | None = None,
    live_owners: Iterable[tuple[str, int, float]] = (),
    ancestors: Iterable[tuple[int, float | None]] | None = None,
    own_pid: int | None = None,
) -> Nesting:
    """Decide whether this process runs inside a bridge job.

    `live_owners` is `(owner_id, pid, started_at)` for every bridge process not marked as exited.
    """
    env = os.environ if environ is None else environ
    parent_job = (env.get(ENV_PARENT_JOB) or "").strip()
    if parent_job:
        return Nesting(
            nested=True,
            via="env",
            parent_job=parent_job,
            parent_owner=(env.get(ENV_PARENT_OWNER) or "").strip() or None,
        )

    me = os.getpid() if own_pid is None else int(own_pid)
    owners_by_pid: dict[int, list[tuple[str, float]]] = {}
    for owner_id, pid, started_at in live_owners:
        if int(pid) == me:
            continue
        owners_by_pid.setdefault(int(pid), []).append((str(owner_id), float(started_at)))
    chain = ancestor_chain(me) if ancestors is None else list(ancestors)
    if chain is None:
        return UNKNOWN_NESTING
    command_line_unavailable = False
    for pid, start_time in chain:
        if pid == me:
            continue
        for owner_id, registered_at in owners_by_pid.get(int(pid), ()):
            # An owner row can outlive its process, and its pid can then be given to anything, a terminal
            # or the host itself included. Taking that for a bridge would refuse every job in an ordinary
            # host. So the ancestor must be shown to be older than the row: a process that started after
            # the row was written, or whose start cannot be read, is not counted.
            if start_time is None or start_time > registered_at + _PID_REUSE_SLACK_SECONDS:
                continue
            return Nesting(nested=True, via="ancestry", parent_owner=owner_id, parent_pid=int(pid))
        argv = _process_command_line(pid)
        if argv is None:
            command_line_unavailable = True
        elif _is_bridge_command(argv) and not _is_own_windows_python_launcher(pid, argv):
            return Nesting(nested=True, via="ancestry", parent_pid=pid)
    return UNKNOWN_NESTING if command_line_unavailable else NOT_NESTED
