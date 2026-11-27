"""`agent-bridge install` and `agent-bridge doctor`.

`install` writes this bridge into a host's MCP configuration, with the tool-call limit and the
approval rule the bridge needs, offers the status line and the skills, and says what is left
to do by hand. For each file it shows a unified diff and asks before writing; it keeps a backup;
it merges and never overwrites; a second run changes nothing. A file it cannot edit safely is left
alone and the snippet to paste is printed instead.

`doctor` only reads. It checks the backend CLIs, each detected host's registration, the state
database and the nesting marker, and prints the fix for each warning or failure.

Every path comes from `hosts.Roots`, built from `--home` when given, so tests and dry runs never
see the real files. Every file access goes through `_read_bytes`, `_write_bytes` and `_exists`,
which call the hooks in `AUDIT` first; the tests use them to fail on any path under the real home.
"""

from __future__ import annotations

import argparse
import base64
import copy
import difflib
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from agent_bridge import hosts as hosts_mod
from agent_bridge.hosts import HOSTS, SERVER_NAME, WANTED_TIMEOUT_SECONDS, Host, Roots, Source
from agent_bridge.nesting import CREDENTIAL_NAME_RE

ENV_PARENT_JOB = "AGENT_BRIDGE_PARENT_JOB"  # the same name as nesting.ENV_PARENT_JOB
# The skills under skills/ that the wheel ships (pyproject.toml force-include) and install offers.
SKILL_NAMES = ("bridge-usage", "bridge-routing")
LAST_STEP = "restart the host, or reconnect agent-bridge in it"
DOCS_WARNING = "written from documentation, not tested on this machine"

BACKEND_FLOORS: dict[str, tuple[int, ...] | None] = {
    "codex": None,
    "agy": (1, 5, 7),
    "claude": (2, 1, 259),
}

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
PathLookup = Callable[[str], str | None]
Ask = Callable[[str], str]
Out = Callable[[str], None]

# --------------------------------------------------------------------------------------------
# File access. Every read and write goes through here, after the audit hooks.

AUDIT: list[Callable[[Path], None]] = []


def _audit(path: Path) -> None:
    for hook in AUDIT:
        hook(path)


def _exists(path: Path) -> bool:
    _audit(path)
    return path.exists()


def _is_dir(path: Path) -> bool:
    _audit(path)
    return path.is_dir()


def _read_bytes(path: Path) -> bytes | None:
    _audit(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


class Unsafe(Exception):
    """The file cannot be edited without risk: print the snippet instead."""


def _edit_mode(path: Path) -> int | None:
    _audit(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise Unsafe(f"refusing symlink {path} -> {path.readlink()}")
    if not stat.S_ISREG(info.st_mode):
        raise Unsafe(f"{path} is not a regular file")
    return stat.S_IMODE(info.st_mode)


def _read_edit_bytes(path: Path) -> bytes | None:
    return None if _edit_mode(path) is None else _read_bytes(path)


def _copy_windows_dacl(source: Path, target: Path) -> None:
    # Windows chmod only handles the read-only flag. Copy the access rules before
    # writing any config data to a new file that would otherwise inherit them.
    import ctypes
    from ctypes import wintypes as win

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    get = advapi.GetNamedSecurityInfoW
    set_ = advapi.SetNamedSecurityInfoW
    get_control = advapi.GetSecurityDescriptorControl
    free = ctypes.WinDLL("kernel32").LocalFree
    pointer = ctypes.c_void_p
    get.argtypes = (win.LPCWSTR, win.DWORD, win.DWORD, pointer, pointer, pointer, pointer, pointer)
    set_.argtypes = (win.LPWSTR, win.DWORD, win.DWORD, pointer, pointer, pointer, pointer)
    get_control.argtypes = (pointer, ctypes.POINTER(win.WORD), ctypes.POINTER(win.DWORD))
    get_control.restype = win.BOOL
    get.restype = set_.restype = win.DWORD
    free.argtypes = (pointer,)
    free.restype = pointer
    file_object = 1
    dacl_information = 0x00000004
    protected_dacl_information = 0x80000000
    dacl_protected = 0x1000
    acl = pointer()
    descriptor = pointer()
    error = get(str(source), file_object, dacl_information, None, None, ctypes.byref(acl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = win.WORD(), win.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if control.value & dacl_protected:
            dacl_information |= protected_dacl_information
        error = set_(str(target), file_object, dacl_information, None, None, acl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        free(descriptor)


def _write_fd(fd: int, data: bytes, path: Path, original: Path | None) -> None:
    with os.fdopen(fd, "wb") as stream:
        if os.name == "nt" and original is not None:
            _copy_windows_dacl(original, path)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _write_bytes(path: Path, data: bytes, old: bytes | None, mode: int | None) -> None:
    _audit(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        _write_fd(fd, data, temp, path if mode is not None else None)
        if mode is not None:
            temp.chmod(mode)
        if _edit_mode(path) != mode or _read_bytes(path) != old:
            raise Unsafe(f"{path} changed while the edit was prepared; run install again")
        os.replace(temp, path)
    except BaseException:
        if os.name == "nt" and mode is not None and not mode & stat.S_IWRITE:
            temp.chmod(stat.S_IWRITE)
        temp.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class Text:
    """A file's text with its newlines made `\\n`, and how to turn edited text back into bytes."""

    text: str
    newline: str = "\n"
    bom: bool = False
    final_newline: bool = True

    @classmethod
    def decode(cls, raw: bytes | None) -> Text | None:
        if raw is None:
            return None
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text = raw.decode("utf-8-sig") if bom else raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Unsafe(f"cannot decode as UTF-8 ({exc.reason} at byte {exc.start})") from None
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        return cls(text, newline, bom, text.endswith("\n") or not text)

    def encode(self, text: str) -> bytes:
        data = text.replace("\n", self.newline).encode("utf-8")
        return b"\xef\xbb\xbf" + data if self.bom else data


def _backup(path: Path, data: bytes, mode: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{path.name}.bak-{stamp}"
    private_mode = mode & 0o600
    suffix = 0
    while True:
        backup = path.with_name(f"{base}-{suffix}" if suffix else base)
        _audit(backup)
        try:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), private_mode)
        except FileExistsError:
            suffix += 1
            continue
        try:
            _write_fd(fd, data, backup, path)
        except BaseException:
            if os.name == "nt" and not private_mode & stat.S_IWRITE:
                backup.chmod(stat.S_IWRITE)
            backup.unlink()
            raise
        return backup


# --------------------------------------------------------------------------------------------
# JSON: merge by setting keys, never by replacing the document.


@dataclass(frozen=True)
class SetKey:
    path: tuple[str, ...]
    value: Any
    mode: Literal["set", "default", "minimum"] = "set"


@dataclass(frozen=True)
class AddToList:
    path: tuple[str, ...]
    item: Any


JsonOp = SetKey | AddToList


def _json_indent(text: str) -> str | None:
    match = re.search(r"\n([ \t]+)\S", text)
    return match.group(1) if match else None


def _dump_json(data: Any, indent: str | None) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False, allow_nan=False)


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Unsafe("duplicate JSON object key")
        result[key] = value
    return result


def _nonfinite_json_number(_token: str) -> Any:
    raise Unsafe("non-finite JSON number")


def _json_number(token: str, parse: type[int] | type[float]) -> int | float:
    try:
        value = parse(token)
    except ValueError:
        raise Unsafe("JSON number is too large to round trip") from None
    if isinstance(value, float) and not math.isfinite(value):
        _nonfinite_json_number(token)
    if json.dumps(value) != token:
        raise Unsafe("a JSON number would change on round trip")
    return value


def load_json(text: str | None) -> tuple[dict[str, Any], str | None, bool]:
    """Parse a JSON object. Raises Unsafe for anything but plain JSON (comments, trailing commas).

    Returns the data, the file's indent, and whether writing it back would re-indent lines the
    edit does not touch (a hand-formatted file); the diff then shows them and a note says so.
    """
    if text is None or not text.strip():
        return {}, "  ", False
    try:
        data = json.loads(
            text,
            parse_int=lambda token: _json_number(token, int),
            parse_float=lambda token: _json_number(token, float),
            parse_constant=_nonfinite_json_number,
            object_pairs_hook=_json_object,
        )
    except json.JSONDecodeError as exc:
        raise Unsafe(f"not plain JSON ({exc.msg} at line {exc.lineno}; comments are not edited)") from None
    if not isinstance(data, dict):
        raise Unsafe("the top level is not a JSON object")
    indent = _json_indent(text)
    return data, indent, _dump_json(data, indent) != text.rstrip("\n")


def get_path(data: Any, path: Sequence[str]) -> Any:
    for key in path:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _above_timeout(current: Any, target: int | str) -> bool:
    try:
        seconds = float(current) if isinstance(current, str) else current
        return isinstance(seconds, (int, float)) and seconds > int(target)
    except ValueError:
        return False


def apply_json_ops(data: dict[str, Any], ops: Iterable[JsonOp]) -> dict[str, Any]:
    new = copy.deepcopy(data)
    for op in ops:
        node = new
        for key in op.path[:-1]:
            child = node.setdefault(key, {})
            if not isinstance(child, dict):
                raise Unsafe(f"{'.'.join(op.path[:-1])} is not an object")
            node = child
        last = op.path[-1]
        if isinstance(op, SetKey):
            if op.mode == "default" and last in node:
                continue
            if op.mode == "minimum" and _above_timeout(node.get(last), op.value):
                continue
            node[last] = copy.deepcopy(op.value)
        else:
            current = node.setdefault(last, [])
            if not isinstance(current, list):
                raise Unsafe(f"{'.'.join(op.path)} is not a list")
            if op.item not in current:
                current.append(op.item)
    return new


def plan_json(text: str | None, ops: Sequence[JsonOp]) -> str | None:
    """The new text, or None when every op is already in place. Raises Unsafe."""
    data, indent, _ = load_json(text)
    new = apply_json_ops(data, ops)
    if new == data:
        return None
    return _dump_json(new, indent or "  ") + "\n"


# --------------------------------------------------------------------------------------------
# TOML: add or edit one table textually, then prove nothing else moved.

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_ANY_HEADER = re.compile(r"^\s*\[\[?[^\[\]]+\]\]?\s*(#.*)?$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if "\\" in value and "'" not in value and "\n" not in value:
            return f"'{value}'"
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} as TOML")


def _trailing_comment(value_text: str) -> str:
    """The `# ...` after a one-line TOML value, found where the text before it parses alone."""
    for i, char in enumerate(value_text):
        if char != "#":
            continue
        try:
            tomllib.loads("v =" + value_text[:i])
        except tomllib.TOMLDecodeError:
            continue
        return value_text[i:].strip()
    return ""


def _header_pattern(table: Sequence[str]) -> re.Pattern[str]:
    parts = [rf"(?:{re.escape(p)}|\"{re.escape(p)}\"|'{re.escape(p)}')" for p in table]
    return re.compile(r"^\s*\[\s*" + r"\s*\.\s*".join(parts) + r"\s*\]\s*(#.*)?$")


def _without(data: dict[str, Any], table: Sequence[str]) -> dict[str, Any]:
    data = copy.deepcopy(data)
    parent = get_path(data, table[:-1])
    if isinstance(parent, dict):
        parent.pop(table[-1], None)
        if not parent and len(table) > 1:
            grand = get_path(data, table[:-2]) if len(table) > 2 else data
            grand.pop(table[-2], None)
    return data


def toml_table_snippet(table: Sequence[str], values: Mapping[str, Any]) -> str:
    lines = ["[" + ".".join(_toml_key(p) for p in table) + "]"]
    lines += [f"{_toml_key(k)} = {toml_value(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def plan_toml(
    text: str | None, table: Sequence[str], values: Mapping[str, Any], *, minimum_key: str | None = None
) -> str | None:
    """Set ``values`` inside the table, adding it at the end if absent. None if all set. Raises Unsafe."""
    text = text or ""
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Unsafe(f"not valid TOML ({exc})") from None
    for end in range(1, len(table)):
        parent = get_path(old, table[:end])
        if parent is not None and not isinstance(parent, dict):
            raise Unsafe(f"{'.'.join(table[:end])} is not a table")
    current = get_path(old, table)
    if current is not None and not isinstance(current, dict):
        raise Unsafe(f"{'.'.join(table)} is not a table")
    if current is not None and minimum_key in values and _above_timeout(current.get(minimum_key), values[minimum_key]):
        values = {key: value for key, value in values.items() if key != minimum_key}
    if current is not None and all(current.get(k) == v for k, v in values.items()):
        return None

    lines = text.split("\n")
    if current is None:
        block = toml_table_snippet(table, values)
        body = text.rstrip("\n")
        new_text = (body + "\n\n" if body else "") + block
    else:
        header = _header_pattern(table)
        start = next((i for i, line in enumerate(lines) if header.match(line)), None)
        if start is None:
            raise Unsafe(f"[{'.'.join(table)}] is written in a form this installer does not edit")
        end = next((i for i in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
        last = start
        for i in range(start + 1, end):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("#"):
                last = i
        missing: list[str] = []
        for key, value in values.items():
            key_re = re.compile(rf"^(\s*)(?:{re.escape(key)}|\"{re.escape(key)}\"|'{re.escape(key)}')\s*=(.*)$")
            hit = next((i for i in range(start + 1, end) if key_re.match(lines[i])), None)
            if hit is None:
                missing.append(f"{_toml_key(key)} = {toml_value(value)}")
                continue
            match = key_re.match(lines[hit])
            assert match is not None
            try:
                tomllib.loads("v =" + match.group(2))
            except tomllib.TOMLDecodeError:
                raise Unsafe(f"{key} spans several lines") from None
            if current.get(key) != value:
                comment = _trailing_comment(match.group(2))
                lines[hit] = f"{match.group(1)}{_toml_key(key)} = {toml_value(value)}" + (f"  {comment}" if comment else "")
        lines[last + 1 : last + 1] = missing
        new_text = "\n".join(lines)

    try:
        new = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise Unsafe(f"the edit would not parse ({exc})") from None
    if _without(new, table) != _without(old, table):
        raise Unsafe("the edit would change another table")
    new_table = get_path(new, table) or {}
    if any(new_table.get(k) != v for k, v in values.items()):
        raise Unsafe("the edit did not set every key")
    untouched = {k: v for k, v in (current or {}).items() if k not in values}
    if {k: v for k, v in new_table.items() if k not in values} != untouched:
        raise Unsafe("the edit would change other keys of the table")
    return new_text


# --------------------------------------------------------------------------------------------
# What a host wants written.

ENTRY_ROOT: dict[str, tuple[str, ...]] = {
    "json-mcpServers": ("mcpServers",),
    "antigravity-mcp_config": ("mcpServers",),
    "vscode-servers": ("servers",),
    "opencode-mcp": ("mcp", "servers"),
    "zed-context_servers": ("context_servers",),
    "toml-mcp_servers": ("mcp_servers",),
}


def entry_path(host: Host) -> tuple[str, ...]:
    return ENTRY_ROOT[host.format] + (SERVER_NAME,)


def is_toml(host: Host) -> bool:
    return host.format.startswith("toml")


def _fill(template: Any, command: str, args: Sequence[str]) -> Any:
    if template == "{command}":
        return command
    if template == "{args}":
        return list(args)
    if template == "{argv}":
        return [command, *args]
    if isinstance(template, dict):
        return {k: _fill(v, command, args) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, command, args) for v in template]
    return template


def usable(source: Source) -> bool:
    return source.kind in ("run", "docs")


def entry_values(host: Host, command: str, args: Sequence[str], approve: bool) -> dict[str, Any]:
    """Keys of the server entry (dotted for nested keys) and the values the bridge wants."""
    values: dict[str, Any] = {}
    for key, template in host.entry.items():
        values[key] = _fill(template, command, args)
    timeout = host.timeout
    if timeout.key and timeout.file is None and timeout.needs_raising() and usable(timeout.source):
        values[timeout.key] = timeout.wanted_value()
    approval = host.approval
    if (
        approval
        and approval.kind == "entry"
        and approval.writable
        and usable(approval.source)
        and (approval.by_default or approve)
    ):
        values[approval.key] = approval.value
    return values


def entry_snippet(host: Host, values: Mapping[str, Any]) -> str:
    if is_toml(host):
        return toml_table_snippet(entry_path(host), values)
    nested: dict[str, Any] = {}
    for key, value in values.items():
        node = nested
        *parents, last = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    doc: Any = nested
    for key in reversed(entry_path(host)):
        doc = {key: doc}
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _powershell_literal(word: str) -> str:
    return "'" + word.replace("'", "''") + "'"


def _powershell_word(word: str) -> str:
    return word if re.fullmatch(r"[\w./:=+@-]+", word) else _powershell_literal(word)


def _manual_command(argv: Sequence[str]) -> str:
    if os.name != "nt":
        return shlex.join(argv)
    parts = [_powershell_word(part) for part in argv]
    return ("& " if parts[0].startswith("'") else "") + " ".join(parts)


def _windows_status_command(argv: Sequence[str]) -> str:
    # A simple command works in cmd, Git Bash and PowerShell. For other arguments,
    # use an encoded PowerShell script so the host shell only sees literal words.
    if all(re.fullmatch(r"[a-zA-Z0-9_./:=+@-]+", part) for part in argv):
        return " ".join(argv)
    script = (
        "$start = New-Object System.Diagnostics.ProcessStartInfo\n"
        f"$start.FileName = {_powershell_literal(argv[0])}\n"
        f"$start.Arguments = {_powershell_literal(subprocess.list2cmdline(argv[1:]))}\n"
        "$start.UseShellExecute = $false\n"
        "$child = [System.Diagnostics.Process]::Start($start)\n"
        "$child.WaitForExit()\n"
        "exit $child.ExitCode\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {encoded}"


def status_command(command: str, args: Sequence[str], *, script: str = "") -> str:
    launch_args = args[:-1] if args and args[-1] == "serve" else args
    argv = [command, *launch_args, "status", "--line"]
    script = script.lower()
    if ".ps1" in script:
        if os.name == "nt" and any(not arg or '"' in arg for arg in argv[1:]):
            return _windows_status_command(argv)
        return "& " + " ".join([_powershell_literal(command), *map(_powershell_word, argv[1:])])
    if os.name != "nt" or ".sh" in script or "bash " in script:
        return shlex.join(argv)
    argv[0] = argv[0].replace("\\", "/")
    return _windows_status_command(argv)


def skill_label(name: str) -> str:
    """The label of a skill's file edit: "usage skill" for bridge-usage."""
    return name.removeprefix("bridge-") + " skill"


def packaged_skill(name: str) -> bytes:
    """The skill's SKILL.md shipped in the wheel, or the checkout's copy in a source install."""
    from importlib import resources

    try:
        resource = resources.files("agent_bridge") / "skills" / name / "SKILL.md"
        if resource.is_file():
            return resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    checkout = Path(__file__).resolve().parents[2] / "skills" / name / "SKILL.md"
    return checkout.read_bytes()


# --------------------------------------------------------------------------------------------
# A plan: the edits for one host, built from what is on disk now.


_REDACTED = "<redacted>"
_CREDENTIAL_TEXT = re.compile(
    r"\bBearer[ \t]+[A-Za-z0-9._~+/\-=]+"
    r"""|\b[a-z][a-z0-9+.-]*://[^\s/\\?#<>"'@]+@[^\s\\<>"']+""",
    re.IGNORECASE,
)
_TOML_ASSIGNMENT = re.compile(r"""^(?:[^"'=\n]|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')+=""")


def _redact_tokens(text: str) -> str:
    return _CREDENTIAL_TEXT.sub(_REDACTED, text)


def _redact_values(
    value: Any, path: tuple[str, ...] = (), visible: frozenset[tuple[str, ...]] = frozenset()
) -> Any:
    if isinstance(value, dict):
        private_values = any(key.casefold() in {"env", "headers"} for key in path)
        return {
            key: _REDACTED if private_values and (*path, key) not in visible
            else _redact_values(item, (*path, key), visible)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_values(item, path, visible) for item in value]
    if isinstance(value, str):
        if any(CREDENTIAL_NAME_RE.search(key) or key.casefold() == "authorization" for key in path):
            return _REDACTED
        return _redact_tokens(value)
    return value


def _json_previews(old: str, new: str, ops: Iterable[JsonOp]) -> tuple[str, str]:
    old_data = json.loads(old) if old.strip() else {}
    new_data = json.loads(new)
    added = frozenset(
        op.path for op in ops if isinstance(op, SetKey)
        and get_path(old_data, op.path) != op.value and get_path(new_data, op.path) == op.value
    )
    return (
        _dump_json(_redact_values(old_data), "  ") + "\n" if old.strip() else "",
        _dump_json(_redact_values(new_data, visible=added), "  ") + "\n",
    )


def _toml_preview(text: str) -> str:
    """Keep TOML's lines and comments. Hide an entire assignment if any of its values are private."""
    lines = iter(text.splitlines(keepends=True))
    preview: list[str] = []
    table: tuple[str, ...] = ()
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            preview.append(line)
            continue
        statement = line
        while True:
            try:
                values = tomllib.loads(statement)
                break
            except tomllib.TOMLDecodeError:
                continuation = next(lines, None)
                if continuation is None:
                    return "".join(preview) + _REDACTED + "\n"
                statement += continuation
        if line.lstrip().startswith("["):
            # Parsing the header resolves quoted and escaped keys, including dots inside quotes.
            table = ()
            while values:
                key, values = next(iter(values.items()))
                table += (key,)
                if isinstance(values, list):
                    values = values[0]
        elif values != _redact_values(values, table):
            match = _TOML_ASSIGNMENT.match(line)
            prefix = f"{match[0]} " if match else ""
            statement = f'{prefix}"{_REDACTED}"\n' + "\n" * (statement.count("\n") - 1)
        preview.append(statement)
    return "".join(preview)


@dataclass
class FileEdit:
    path: Path
    labels: list[str]
    raw_old: bytes | None
    new_bytes: bytes
    old_preview: str
    new_preview: str


@dataclass
class Snippet:
    where: str
    text: str
    reason: str


@dataclass
class HostReport:
    host: Host
    done: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    printed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class Options:
    command: str
    args: list[str]
    scope: str
    approve: bool
    status_line: bool
    skill: bool
    print_only: bool
    yes: bool


class Planner:
    """Collects the edits for one host, file by file, with the labels each one carries."""

    def __init__(self, roots: Roots, opts: Options) -> None:
        self.roots = roots
        self.opts = opts
        self.json_ops: dict[Path, list[tuple[str, JsonOp]]] = {}
        self.toml_ops: dict[Path, tuple[str, tuple[str, ...], dict[str, Any], str | None]] = {}
        self.raw_files: dict[Path, tuple[str, bytes]] = {}
        self.snippets: list[Snippet] = []
        self.already: list[str] = []

    def add_json(self, path: Path, label: str, op: JsonOp) -> None:
        self.json_ops.setdefault(path, []).append((label, op))

    def add_toml(self, path: Path, label: str, table: tuple[str, ...], values: dict[str, Any], minimum_key: str | None) -> None:
        self.toml_ops[path] = (label, table, values, minimum_key)

    def edits(self) -> list[FileEdit]:
        edits: list[FileEdit] = []
        for path, items in self.json_ops.items():
            labels = list(dict.fromkeys(label for label, _ in items))
            try:
                raw = _read_edit_bytes(path)
                text = Text.decode(raw) or Text("")
                new = plan_json(text.text, [op for _, op in items])
                if new is None:
                    self.already += [f"{label} ({path})" for label in labels]
                    continue
                if not text.final_newline:
                    new = new.rstrip("\n")
                new_bytes = text.encode(new)
                reformats = load_json(text.text)[2]
                old_preview, new_preview = _json_previews(text.text, new, (op for _, op in items))
            except (Unsafe, OSError, UnicodeEncodeError, RecursionError) as exc:
                self.snippets.append(
                    Snippet(str(path), _json_snippet_for(items), f"{path.name} not edited: {exc}")
                )
                continue
            if reformats:
                labels.append("re-indents lines it does not change: read the whole diff")
            edits.append(FileEdit(path, labels, raw, new_bytes, old_preview, new_preview))
        for path, (label, table, values, minimum_key) in self.toml_ops.items():
            try:
                raw = _read_edit_bytes(path)
                text = Text.decode(raw) or Text("")
                new = plan_toml(text.text, table, values, minimum_key=minimum_key)
            except (Unsafe, OSError) as exc:
                self.snippets.append(
                    Snippet(str(path), toml_table_snippet(table, values), f"{path.name} not edited: {exc}")
                )
                continue
            if new is None:
                self.already.append(f"{label} ({path})")
                continue
            edits.append(FileEdit(path, [label], raw, text.encode(new), _toml_preview(text.text), _toml_preview(new)))
        for path, (label, data) in self.raw_files.items():
            try:
                raw = _read_edit_bytes(path)
            except (Unsafe, OSError) as exc:
                self.snippets.append(Snippet(str(path), data.decode("utf-8"), f"{path.name} not edited: {exc}"))
                continue
            if raw is not None and raw.replace(b"\r\n", b"\n") == data.replace(b"\r\n", b"\n"):
                self.already.append(f"{label} ({path})")
                continue
            old_text = raw.decode("utf-8", "replace").replace("\r\n", "\n") if raw is not None else ""
            new_text = data.decode("utf-8", "replace").replace("\r\n", "\n")
            edits.append(FileEdit(path, [label], raw, data, old_text, new_text))
        return edits


def _json_snippet_for(items: Sequence[tuple[str, JsonOp]]) -> str:
    try:
        new = apply_json_ops({}, [op for _, op in items])
    except Unsafe:
        new = {}
    return json.dumps(new, indent=2, ensure_ascii=False) + "\n"


def config_path(host: Host, roots: Roots, scope: str) -> tuple[Path | None, str | None]:
    """The host's MCP file for the scope, or None with the reason it cannot be written."""
    template = host.user_config if scope == "user" else host.project_config
    if template is None:
        return None, f"{host.label} has no documented {scope}-scope MCP file"
    source = host.config_path_source(scope)
    if not usable(source):
        note = f": {source.note}" if source.note else ""
        return roots.path(template), f"the {scope}-scope file is not verified{note}"
    return roots.path(template), None


def plan_registration(host: Host, roots: Roots, opts: Options, planner: Planner) -> Path | None:
    path, why_not = config_path(host, roots, opts.scope)
    values = entry_values(host, opts.command, opts.args, opts.approve)
    if why_not is not None:
        where = str(path) if path else f"{host.label}'s MCP settings"
        planner.snippets.append(Snippet(where, entry_snippet(host, values), why_not))
        return None
    assert path is not None
    timeout_key = host.timeout.key if host.timeout.file is None else None
    if is_toml(host):
        planner.add_toml(path, "registration", entry_path(host), values, timeout_key)
    else:
        for key, value in values.items():
            mode = "minimum" if key == timeout_key else "default" if value == {} else "set"
            planner.add_json(path, "registration", SetKey(entry_path(host) + tuple(key.split(".")), value, mode))
    return path


def plan_side_files(host: Host, roots: Roots, opts: Options, planner: Planner) -> None:
    timeout = host.timeout
    if timeout.key and timeout.file and timeout.needs_raising() and usable(timeout.source):
        planner.add_json(roots.path(timeout.file), "tool-call limit", SetKey(tuple(timeout.key.split(".")), timeout.wanted_value(), "minimum"))
    approval = host.approval
    if approval and approval.kind == "list" and (approval.by_default or opts.approve):
        if approval.writable and approval.file and usable(approval.source):
            planner.add_json(roots.path(approval.file), "approval rule", AddToList(tuple(approval.key.split(".")), approval.value))
        else:
            planner.snippets.append(
                Snippet(roots.path(approval.file).as_posix() if approval.file else host.label, approval.snippet, "the approval rule is not written by the installer")
            )


def plan_status_line(host: Host, roots: Roots, opts: Options, planner: Planner, registration: Path | None) -> None:
    spec = host.status_line
    if spec is None or not opts.status_line or opts.scope != "user":
        return
    status_cmd = status_command(opts.command, opts.args)
    path = roots.path(spec.file)
    key = tuple(spec.key.split("."))
    writable = spec.writable and usable(spec.source)
    manual = spec.snippet.replace("<status command>", json.dumps(status_cmd)[1:-1])
    try:
        text = Text.decode(_read_edit_bytes(path)) or Text("")
        data = tomllib.loads(text.text) if path.suffix == ".toml" else load_json(text.text)[0]
    except (Unsafe, OSError, tomllib.TOMLDecodeError) as exc:
        snippet = manual or json.dumps({spec.key: _status_value(spec, status_cmd)}, indent=2)
        planner.snippets.append(Snippet(str(path), snippet, f"{path.name} not edited: {exc}"))
        return
    existing = get_path(data, key)
    if existing is None:
        if writable:
            planner.add_json(path, "status line", SetKey(key, _status_value(spec, status_cmd)))
        else:
            planner.snippets.append(Snippet(str(path), manual, "the status line is not written by the installer"))
        return
    existing_cmd = existing.get("command", "") if isinstance(existing, dict) else existing
    if not isinstance(existing_cmd, str):
        existing_cmd = ""
    if existing_cmd == status_cmd:
        planner.already.append(f"status line ({path})")
        return
    previous = None
    if registration is not None:
        try:
            if _edit_mode(registration) is not None:
                previous = registered_entry(host, registration)
        except (Unsafe, OSError):
            pass
    old_command = _entry_command(host, previous)
    old_args = get_path(previous, ("args",))
    if isinstance(get_path(previous, ("command",)), list):
        old_args = previous["command"][1:]
    generated = old_command and isinstance(old_args, list) and all(isinstance(arg, str) for arg in old_args)
    try:
        argv = _split_command(existing_cmd)
    except (OSError, ValueError):
        argv = []
    direct = argv[-2:] == ["status", "--line"] and (
        argv[1:3] == ["-m", "agent_bridge"] or Path(argv[0]).stem == "agent-bridge"
    )
    if direct or (generated and existing_cmd == status_command(old_command, old_args)):
        old_path = Path(old_command) if old_command else None
        if old_path is not None and old_path != Path(opts.command) and (
            not direct or Path(argv[0]) == old_path
        ):
            planner.snippets.append(Snippet(
                str(path), status_cmd,
                f"the existing status line still uses the old command `{existing_cmd}`; replace its command with this line",
            ))
        else:
            planner.already.append(f"status line ({path})")
        return
    planner.snippets.append(
        Snippet(
            existing_cmd or str(path),
            status_command(opts.command, opts.args, script=existing_cmd) + "\n",
            "a status line is already set and is kept: add this line to your own script so it prints the bridge's line too",
        )
    )


def _status_value(spec: hosts_mod.StatusLine, status_cmd: str) -> dict[str, Any]:
    return {k: (status_cmd if v == "{status_cmd}" else v) for k, v in spec.value.items()}


def plan_skill(host: Host, roots: Roots, opts: Options, planner: Planner, seen: set[Path]) -> None:
    if not opts.skill:
        return
    template = host.skills_user if opts.scope == "user" else host.skills_project
    if template is None or not usable(host.skills_source):
        return
    for name in SKILL_NAMES:
        path = roots.path(template) / name / "SKILL.md"
        if path in seen:
            continue
        seen.add(path)
        planner.raw_files[path] = (skill_label(name), packaged_skill(name))


def expand_register(argv: Sequence[str], opts: Options) -> list[str]:
    out: list[str] = []
    for part in argv:
        if part == "{cmd}":
            out += [opts.command, *opts.args]
        elif part == "{scope}":
            out.append(opts.scope)
        elif part == "{vscode_json}":
            out.append(json.dumps({"name": SERVER_NAME, "type": "stdio", "command": opts.command, "args": opts.args}))
        else:
            out.append(part)
    return out


def registered_entry(host: Host, path: Path) -> Any:
    """The bridge's entry in a host's MCP file, None if absent. Raises Unsafe if unreadable."""
    text = Text.decode(_read_bytes(path))
    if text is None:
        return None
    if is_toml(host):
        try:
            data = tomllib.loads(text.text)
        except tomllib.TOMLDecodeError as exc:
            raise Unsafe(f"not valid TOML ({exc})") from None
    else:
        try:
            data = json.loads(text.text) if text.text.strip() else {}
        except json.JSONDecodeError as exc:
            raise Unsafe(f"not plain JSON ({exc.msg} at line {exc.lineno})") from None
        except (ValueError, RecursionError) as exc:
            raise Unsafe(f"cannot read JSON ({exc})") from None
    return get_path(data, entry_path(host))


# --------------------------------------------------------------------------------------------
# install


class Session:
    def __init__(
        self,
        opts: Options,
        roots: Roots,
        out: Out,
        ask: Ask,
        path_lookup: PathLookup,
    ) -> None:
        self.opts = opts
        self.roots = roots
        self.out = out
        self.ask = ask
        self.path_lookup = path_lookup
        self.skill_paths: set[Path] = set()

    def confirm(self, prompt: str) -> bool:
        if self.opts.print_only:
            return False
        if self.opts.yes:
            return True
        try:
            answer = self.ask(f"{prompt} [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def show_diff(self, edit: FileEdit) -> None:
        label = ", ".join(edit.labels)
        state = "new file" if edit.raw_old is None else "changes"
        self.out(f">> {edit.path} ({label}; {state})")
        if edit.raw_old is None and edit.labels in ([skill_label(name)] for name in SKILL_NAMES):
            self.out(f"(the packaged {edit.path.parent.name}/SKILL.md, {len(edit.new_preview.splitlines())} lines)")
            return
        diff = difflib.unified_diff(
            edit.old_preview.splitlines(keepends=True),
            edit.new_preview.splitlines(keepends=True),
            fromfile=str(edit.path) if edit.raw_old is not None else "/dev/null",
            tofile=str(edit.path),
        )
        self.out("".join(diff).rstrip("\n"))

    def apply(self, edit: FileEdit, report: HostReport) -> None:
        self.show_diff(edit)
        label = ", ".join(edit.labels)
        if self.opts.print_only:
            report.printed.append(f"{label}: diff shown, nothing written ({edit.path})")
            return
        if not self.confirm("Apply?"):
            report.declined.append(f"{label} ({edit.path})")
            return
        try:
            mode = _edit_mode(edit.path)
            if _read_bytes(edit.path) != edit.raw_old or (mode is None) != (edit.raw_old is None):
                raise Unsafe(f"{edit.path} changed while the diff was shown; run install again")
            if edit.raw_old is not None and mode is not None:
                self.out(f"backup: {_backup(edit.path, edit.raw_old, mode)}")
            _write_bytes(edit.path, edit.new_bytes, edit.raw_old, mode)
        except (Unsafe, OSError) as exc:
            report.failed.append(f"{label}: {exc}")
            return
        report.done.append(f"{label} ({edit.path})")

    def install_host(self, host: Host) -> HostReport:
        report = HostReport(host)
        opts = self.opts
        self.out(f"\n== {host.label} ({host.name}) ==")
        source = host.config_path_source(opts.scope)
        if source.kind == "docs":
            self.out(f"note: {DOCS_WARNING}. Source: {source.url}")
        for note in host.notes:
            self.out(f"note: {note}")
        if not host.timeout.raisable:
            limit = host.timeout.default_seconds
            self.out(
                f"note: {host.label}'s tool-call limit cannot be raised"
                + (f" ({limit:g} s)" if limit else "")
                + ": run answers at once, and jobs waits under the limit"
            )
        elif host.timeout.key is None and host.timeout.needs_raising():
            self.out(f"note: no verified key raises {host.label}'s tool-call limit; set it to {WANTED_TIMEOUT_SECONDS} s in its settings if it has one")

        planner = Planner(self.roots, opts)
        registration = plan_registration(host, self.roots, opts, planner)
        plan_side_files(host, self.roots, opts, planner)
        plan_status_line(host, self.roots, opts, planner, registration)
        plan_skill(host, self.roots, opts, planner, self.skill_paths)
        for edit in planner.edits():
            self.apply(edit, report)
        report.already += planner.already
        for snippet in planner.snippets:
            self.out(f">> paste by hand into {snippet.where}: {snippet.reason}")
            self.out(snippet.text.rstrip("\n"))
            report.printed.append(f"snippet for {snippet.where}")
        reg = host.register
        if reg and usable(reg.source) and opts.scope in reg.scopes:
            binary = self.path_lookup(reg.argv[0])
            batch = binary and hosts_mod.is_batch_launcher(binary)
            if batch:
                self.out(f"note: {binary} is a Windows batch launcher")
            if registration is None or batch:
                self.out("or run manually: " + _manual_command(expand_register(reg.argv, opts)))
        return report


def _split_command(raw: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(raw)

    import ctypes
    from ctypes import wintypes

    parse = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.WinDLL("kernel32").LocalFree
    free.argtypes = (ctypes.c_void_p,)
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    # CommandLineToArgvW gives argv[0] special rules. Parse user arguments after it.
    argv = parse("agent-bridge " + raw, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return argv[1:count.value]
    finally:
        free(argv)


def default_command() -> tuple[str, list[str]]:
    """The command that starts this bridge: the running interpreter with `-m agent_bridge`."""
    return os.path.abspath(sys.executable), ["-m", "agent_bridge"]


def _linked_worktree(path: Path) -> bool:
    for parent in path.parents:
        marker = parent / ".git"
        if marker.is_dir():
            return False
        if marker.is_file():
            line = marker.read_text(encoding="utf-8").strip()
            if line.startswith("gitdir: ") and (parent / line.removeprefix("gitdir: ") / "commondir").is_file():
                return True
    return False


def _transient_command(command: str, roots: Roots, env: Mapping[str, str]) -> str | None:
    path = Path(command).expanduser()
    if not path.is_absolute() and not path.is_file():
        return None
    path = path.absolute()
    try:
        paths = (path, path.resolve())
    except (OSError, RuntimeError):
        paths = (path,)
    if any(
        part.lower() == "uvx" or re.fullmatch(r"(?:archive|environments)-v\d+", part.lower())
        for candidate in paths for part in candidate.parent.parts
    ):
        return "a uvx cache"
    try:
        if any(_linked_worktree(candidate) for candidate in dict.fromkeys(paths)):
            return "a linked Git worktree"
    except (OSError, UnicodeError):
        pass
    temp_dirs = [Path(env[name]).absolute() for name in ("TMPDIR", "TMP", "TEMP") if env.get(name)]
    if os.name == "nt":
        temp_dirs.append(roots.home / "AppData" / "Local" / "Temp")
    else:
        temp_dirs.extend(map(Path, ("/tmp", "/var/tmp", "/private/tmp")))
    if any(candidate.is_relative_to(directory) for candidate in paths for directory in temp_dirs):
        return "a temporary folder"
    return None


def _install_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-bridge install",
        description="Register this bridge in a host CLI's MCP configuration. Shows a diff and asks before each write.",
    )
    names = [h.name for h in HOSTS]
    parser.add_argument("--host", action="append", choices=names, metavar="NAME", help="host to set up (repeatable): " + ", ".join(names) + ". Default: offer each host found on this machine")
    parser.add_argument("--scope", choices=("user", "project"), default="user", help="user (every project, the default) or project (the current folder)")
    parser.add_argument("--project", type=Path, help="the project folder for --scope project (default: the current folder)")
    parser.add_argument("--command", help="the command that starts the bridge (default: this Python with -m agent_bridge)")
    parser.add_argument("--print", dest="print_only", action="store_true", help="show the diffs and write nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="accept every change without asking")
    parser.add_argument("--approve", action="store_true", help="also pre-approve the bridge's tools where the host does not need it for headless runs")
    parser.add_argument("--no-status-line", dest="status_line", action="store_false", help="do not offer the status line")
    parser.add_argument("--no-skill", dest="skill", action="store_false", help="do not offer the skills")
    parser.add_argument("--home", type=Path, help="(for tests) treat DIR as the home folder; the real one is never read or written, and no host command is run")
    return parser


def run_install(
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    path_lookup: PathLookup = shutil.which,
    runner: Runner | None = None,
    ask: Ask = input,
    out: Out = print,
) -> int:
    """`agent-bridge install`. Registration uses file edits or manual instructions.

    The optional runner is retained for callers of this entry point. Install never invokes it.
    """
    args = _install_parser().parse_args(argv)
    emit = out

    def out(message: str) -> None:
        emit(_redact_tokens(message))

    env = dict(os.environ) if env is None else dict(env)
    if args.command:
        try:
            parts = _split_command(args.command)
        except (OSError, ValueError) as exc:
            out(f"agent-bridge install: cannot parse --command ({exc})")
            return 2
        if not parts or not parts[0]:
            out("agent-bridge install: --command is empty")
            return 2
        command, cmd_args = parts[0], parts[1:]
    else:
        command, cmd_args = default_command()
    if args.home is not None:
        roots = Roots.for_home(args.home, project=args.project)
    else:
        roots = Roots.from_env(env, project=args.project)
    opts = Options(
        command=command,
        args=cmd_args,
        scope=args.scope,
        approve=args.approve,
        status_line=args.status_line,
        skill=args.skill,
        print_only=args.print_only,
        yes=args.yes,
    )
    session = Session(opts, roots, out, ask, path_lookup)
    out(f"command: {' '.join([command, *cmd_args])}")
    executable = command
    if not re.search(r"[/\\]", command):
        executable = path_lookup(command) or command
    location = _transient_command(executable, roots, env)
    if location:
        out(f"warning: {command} is in {location}; use --command with a stable path if it may be removed")
    if args.print_only:
        out("--print: nothing will be written and no command will be run")

    if args.host:
        chosen = [hosts_mod.get_host(name) for name in dict.fromkeys(args.host)]
    else:
        found = hosts_mod.detect_installed(env, path_lookup, roots=roots, exists=_exists)
        if not found:
            out("No host found on this machine. Name one with --host: " + ", ".join(h.name for h in HOSTS))
            return 1
        out("Hosts found: " + ", ".join(h.label for h in found))
        chosen = [h for h in found if opts.print_only or opts.yes or session.confirm(f"Set up {h.label}?")]

    reports = [session.install_host(host) for host in chosen]

    out("\n== Summary ==")
    for report in reports:
        out(f"{report.host.label}:")
        for title, items in (
            ("done", report.done),
            ("already set", report.already),
            ("declined", report.declined),
            ("shown only", report.printed),
            ("failed", report.failed),
        ):
            for item in items:
                out(f"  {title}: {item}")
        if not any((report.done, report.already, report.declined, report.printed, report.failed)):
            out("  nothing to do")
    if any(r.done for r in reports):
        out(f"\nOne step left: {LAST_STEP}.")
    elif reports and not opts.print_only:
        out(f"\nIf you changed anything by hand: {LAST_STEP}.")
    return 1 if any(r.failed for r in reports) else 0


# --------------------------------------------------------------------------------------------
# doctor


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(p) for p in match.groups()) if match else None


def _version_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=20, check=False)


def check_backends(env: Mapping[str, str], path_lookup: PathLookup, runner: Runner) -> list[Check]:
    checks: list[Check] = []
    found_any = False
    for name, floor in BACKEND_FLOORS.items():
        override = (env.get(f"AGENT_BRIDGE_{name.upper()}_BIN") or "").strip()
        binary = override or path_lookup(name)
        label = f"backend {name}"
        if not binary or hosts_mod.is_shim(binary):
            checks.append(Check(label, "warn", "not found on PATH", f"install {name}, or set AGENT_BRIDGE_{name.upper()}_BIN"))
            continue
        found_any = True
        if hosts_mod.is_batch_launcher(binary):
            checks.append(Check(label, "warn", f"{binary} is a Windows batch launcher", "run manually: " + _manual_command([binary, "--version"])))
            continue
        try:
            result = runner([binary, "--version"])
            version = parse_version((result.stdout or "") + (result.stderr or ""))
        except (OSError, subprocess.SubprocessError) as exc:
            checks.append(Check(label, "fail", f"{binary}: --version failed ({exc})", f"reinstall {name}"))
            continue
        if version is None:
            checks.append(Check(label, "warn", f"{binary}: no version in its --version output", f"check `{name} --version`"))
            continue
        shown = ".".join(map(str, version))
        if floor and version < floor:
            need = ".".join(map(str, floor))
            checks.append(Check(label, "fail", f"{shown}, older than {need}", f"update {name} to {need} or newer"))
        else:
            extra = f" (needs {'.'.join(map(str, floor))}+)" if floor else ""
            checks.append(Check(label, "ok", f"{shown}{extra}"))
    if not found_any:
        checks.append(Check("backends", "fail", "none of codex, agy, claude was found", "install at least one backend CLI"))
    return checks


def _timeout_seconds(host: Host, entry: Any, roots: Roots) -> float | None:
    spec = host.timeout
    if spec.key is None:
        return None
    if spec.file:
        text = Text.decode(_read_bytes(roots.path(spec.file)))
        try:
            data = json.loads(text.text) if text and text.text.strip() else {}
        except json.JSONDecodeError:
            return None
        raw = get_path(data, spec.key.split("."))
    else:
        raw = get_path(entry, spec.key.split(".")) if isinstance(entry, dict) else None
    try:
        return float(raw) / spec.scale if raw is not None else None
    except (TypeError, ValueError):
        return None


def configured_timeout_seconds(host: Host, entry: Any, roots: Roots) -> float | None:
    """Read a timeout override using the same parser as doctor."""
    return _timeout_seconds(host, entry, roots)


def _approval_present(host: Host, entry: Any, roots: Roots) -> bool | None:
    approval = host.approval
    if approval is None:
        return None
    if approval.kind == "entry":
        if not approval.key:
            return None
        return isinstance(entry, dict) and get_path(entry, approval.key.split(".")) == approval.value
    if not approval.file:
        return None
    raw = _read_bytes(roots.path(approval.file))
    text = Text.decode(raw)
    if text is None:
        return False
    try:
        data = tomllib.loads(text.text) if approval.file_format == "toml" else json.loads(text.text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError):
        return None
    current = get_path(data, approval.key.split("."))
    return isinstance(current, list) and approval.value in current


def _entry_command(host: Host, entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if isinstance(command, list) and command:
        command = command[0]
    return command if isinstance(command, str) else None


def check_host(host: Host, roots: Roots, path_lookup: PathLookup, skills: Mapping[str, bytes]) -> list[Check]:
    fix = f"agent-bridge install --host {host.name}"
    checks: list[Check] = []
    label = host.label
    entry: Any = None
    where: Path | None = None
    for scope in ("user", "project"):
        path, why_not = config_path(host, roots, scope)
        if path is None or why_not is not None or not _exists(path):
            continue
        try:
            found = registered_entry(host, path)
        except Unsafe as exc:
            checks.append(Check(f"{label}: config", "warn", f"cannot read {path}: {exc}", f"fix the file, then {fix}"))
            continue
        if found is not None:
            entry, where = found, path
            break
    user_path, why_not = config_path(host, roots, "user")
    if entry is None:
        if why_not is not None:
            checks.append(Check(f"{label}: registered", "warn", f"cannot check: {why_not}", fix + " (prints the snippet)"))
            return checks
        checks.append(Check(f"{label}: registered", "warn", f"not registered in {user_path}", fix))
    else:
        checks.append(Check(f"{label}: registered", "ok", str(where)))
        command = _entry_command(host, entry)
        if command and (Path(command).is_absolute() and _exists(Path(command)) or not Path(command).is_absolute() and path_lookup(command)):
            checks.append(Check(f"{label}: command", "ok", command))
        else:
            checks.append(Check(f"{label}: command", "fail", f"{command or '(none)'} does not exist", fix))
        spec = host.timeout
        if not spec.raisable:
            limit = f"{spec.default_seconds:g} s" if spec.default_seconds else "unknown"
            checks.append(Check(f"{label}: tool-call limit", "warn", f"fixed at {limit}, cannot be raised", "run answers at once, and jobs waits under the limit"))
        elif not spec.needs_raising():
            checks.append(Check(f"{label}: tool-call limit", "ok", f"default {spec.default_seconds:g} s is enough"))
        elif spec.key is None:
            checks.append(Check(f"{label}: tool-call limit", "warn", f"no verified key; default {spec.default_seconds or 'unknown'} s", f"raise it to {WANTED_TIMEOUT_SECONDS} s in {label}'s settings"))
        else:
            seconds = _timeout_seconds(host, entry, roots)
            default = f"{spec.default_seconds:g} s" if spec.default_seconds else "not documented"
            if seconds is not None and seconds >= WANTED_TIMEOUT_SECONDS:
                checks.append(Check(f"{label}: tool-call limit", "ok", f"{spec.key} = {seconds:g} s"))
            else:
                now = f"{seconds:g} s" if seconds is not None else f"unset, so the default ({default})"
                checks.append(Check(f"{label}: tool-call limit", "warn", f"{spec.key} is {now}; the bridge wants {WANTED_TIMEOUT_SECONDS} s", fix))
    approval = host.approval
    if approval and approval.headless_needs:
        present = _approval_present(host, entry, roots)
        if present:
            checks.append(Check(f"{label}: approval", "ok", f"{approval.key} has {approval.value}"))
        else:
            checks.append(Check(f"{label}: approval", "warn", f"{approval.key} lacks {approval.value!r}: headless runs fail on bridge tools", fix))
    if host.skills_user and usable(host.skills_source):
        for name, skill in skills.items():
            skill_path = roots.path(host.skills_user) / name / "SKILL.md"
            current = _read_bytes(skill_path)
            if current is None:
                checks.append(Check(f"{label}: skill {name}", "warn", f"missing at {skill_path}", fix))
            elif current.replace(b"\r\n", b"\n") != skill.replace(b"\r\n", b"\n"):
                checks.append(Check(f"{label}: skill {name}", "warn", f"out of date at {skill_path}", fix))
            else:
                checks.append(Check(f"{label}: skill {name}", "ok", str(skill_path)))
    return checks


def check_state(state_dir: Path) -> list[Check]:
    state_dir = state_dir.resolve()
    checks: list[Check] = []
    if not _is_dir(state_dir):
        return [Check("state folder", "warn", f"{state_dir} does not exist yet", "it is made on the bridge's first start")]
    checks.append(Check("state folder", "ok", str(state_dir)))
    db = state_dir / "bridge.db"
    if not _exists(db):
        checks.append(Check("database", "warn", f"{db} does not exist yet", "it is made on the bridge's first start"))
        return checks
    _audit(db)
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            tables = conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        checks.append(Check("database", "fail", f"{db} does not open read-only: {exc}", "move it aside; the bridge makes a new one"))
        return checks
    checks.append(Check("database", "ok", f"{db} opens read-only, {tables} tables"))
    return checks


def run_doctor(
    argv: list[str],
    *,
    env: Mapping[str, str] | None = None,
    path_lookup: PathLookup = shutil.which,
    runner: Runner | None = None,
    out: Out = print,
) -> int:
    """`agent-bridge doctor`: read-only checks, one row each. Exit 1 if any fails."""
    parser = argparse.ArgumentParser(prog="agent-bridge doctor", description="Check the backends, the host registrations and the state folder. Changes nothing.")
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")
    parser.add_argument("--home", type=Path, help="(for tests) treat DIR as the home folder")
    parser.add_argument("--state-dir", type=Path, help="check this state folder")
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else dict(env)
    if args.home is not None:
        roots = Roots.for_home(args.home)
        state_dir = args.home / ".agent-bridge"
    else:
        roots = Roots.from_env(env)
        raw = (env.get("AGENT_BRIDGE_STATE_DIR") or "").strip()
        state_dir = Path(raw).expanduser() if raw else roots.home / ".agent-bridge"
    checks = check_backends(env, path_lookup, runner or _version_runner)
    skills = {name: packaged_skill(name) for name in SKILL_NAMES}
    detected = hosts_mod.detect_installed(env, path_lookup, roots=roots, exists=_exists)
    if not detected:
        checks.append(Check("hosts", "warn", "no host found on this machine", "agent-bridge install --host NAME"))
    for host in detected:
        checks += check_host(host, roots, path_lookup, skills)
    checks += check_state(args.state_dir.expanduser() if args.state_dir is not None else state_dir)
    if (env.get(ENV_PARENT_JOB) or "").strip():
        checks.append(Check("nesting", "fail", f"{ENV_PARENT_JOB} is set: a bridge started here refuses to start jobs", f"unset {ENV_PARENT_JOB} (it is meant only for a bridge's own subagents)"))
    else:
        checks.append(Check("nesting", "ok", f"{ENV_PARENT_JOB} not set"))

    failed = any(c.status == "fail" for c in checks)
    if args.json:
        out(json.dumps({"ok": not failed, "checks": [c.__dict__ for c in checks]}, indent=2))
        return 1 if failed else 0
    width = max(len(c.name) for c in checks)
    for c in checks:
        out(f"{c.status:<4}  {c.name:<{width}}  {c.detail}")
    fixes = [c for c in checks if c.status != "ok" and c.fix]
    if fixes:
        out("\nTo fix:")
        for c in fixes:
            out(f"  {c.name}: {c.fix}")
    return 1 if failed else 0
