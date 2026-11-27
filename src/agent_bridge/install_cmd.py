"""`agent-bridge install` and `agent-bridge doctor`.

`install` writes this bridge into a host's MCP configuration, with the tool-call limit and the
approval rule the bridge needs, offers the status line and the usage skill, and says what is left
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
import copy
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_bridge import hosts as hosts_mod
from agent_bridge.hosts import HOSTS, SERVER_NAME, WANTED_TIMEOUT_SECONDS, Host, Roots, Source

ENV_PARENT_JOB = "AGENT_BRIDGE_PARENT_JOB"  # the same name as nesting.ENV_PARENT_JOB
SKILL_NAME = "bridge-usage"
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


def _write_bytes(path: Path, data: bytes) -> None:
    _audit(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


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
        text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        return cls(text, newline, bom, text.endswith("\n") or not text)

    def encode(self, text: str) -> bytes:
        data = text.replace("\n", self.newline).encode("utf-8")
        return b"\xef\xbb\xbf" + data if self.bom else data


def backup_path(path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.bak-{stamp}")


# --------------------------------------------------------------------------------------------
# JSON: merge by setting keys, never by replacing the document.


class Unsafe(Exception):
    """The file cannot be edited without risk: print the snippet instead."""


@dataclass(frozen=True)
class SetKey:
    path: tuple[str, ...]
    value: Any


@dataclass(frozen=True)
class AddToList:
    path: tuple[str, ...]
    item: Any


JsonOp = SetKey | AddToList


def _json_indent(text: str) -> str | None:
    match = re.search(r"\n([ \t]+)\S", text)
    return match.group(1) if match else None


def _dump_json(data: Any, indent: str | None) -> str:
    return json.dumps(data, indent=indent, ensure_ascii=False)


def load_json(text: str | None) -> tuple[dict[str, Any], str | None, bool]:
    """Parse a JSON object. Raises Unsafe for anything but plain JSON (comments, trailing commas).

    Returns the data, the file's indent, and whether writing it back would re-indent lines the
    edit does not touch (a hand-formatted file); the diff then shows them and a note says so.
    """
    if text is None or not text.strip():
        return {}, "  ", False
    try:
        data = json.loads(text)
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


def plan_toml(text: str | None, table: Sequence[str], values: Mapping[str, Any]) -> str | None:
    """Set ``values`` inside the table, adding it at the end if absent. None if all set. Raises Unsafe."""
    text = text or ""
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise Unsafe(f"not valid TOML ({exc})") from None
    current = get_path(old, table)
    if current is not None and not isinstance(current, dict):
        raise Unsafe(f"{'.'.join(table)} is not a table")
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


def status_command(command: str, args: Sequence[str]) -> str:
    argv = [command, *args]
    if argv and argv[-1] == "serve":
        argv = argv[:-1]
    argv += ["status", "--line"]
    if os.name == "nt":
        argv[0] = argv[0].replace("\\", "/")
    return " ".join(f'"{part}"' if re.search(r"[\s\\'\"]", part) or part == argv[0] else part for part in argv)


def user_script_line(existing_command: str, status_cmd: str) -> str:
    """The line to add to the user's own status-line script."""
    if ".ps1" in existing_command.lower():
        return f"& {status_cmd}"
    return status_cmd


def packaged_skill() -> bytes:
    """The SKILL.md shipped in the wheel, or the checkout's copy in a source install."""
    from importlib import resources

    try:
        resource = resources.files("agent_bridge") / "skills" / SKILL_NAME / "SKILL.md"
        if resource.is_file():
            return resource.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    checkout = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME / "SKILL.md"
    return checkout.read_bytes()


# --------------------------------------------------------------------------------------------
# A plan: the edits for one host, built from what is on disk now.


@dataclass
class FileEdit:
    path: Path
    labels: list[str]
    raw_old: bytes | None
    new_bytes: bytes
    old_text: str
    new_text: str


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
        self.toml_ops: dict[Path, list[tuple[str, tuple[str, ...], dict[str, Any]]]] = {}
        self.raw_files: dict[Path, tuple[str, bytes]] = {}
        self.snippets: list[Snippet] = []
        self.already: list[str] = []

    def add_json(self, path: Path, label: str, op: JsonOp) -> None:
        self.json_ops.setdefault(path, []).append((label, op))

    def add_toml(self, path: Path, label: str, table: tuple[str, ...], values: dict[str, Any]) -> None:
        self.toml_ops.setdefault(path, []).append((label, table, values))

    def edits(self) -> list[FileEdit]:
        edits: list[FileEdit] = []
        for path, items in self.json_ops.items():
            raw = _read_bytes(path)
            text = Text.decode(raw)
            old = text.text if text else None
            labels = list(dict.fromkeys(label for label, _ in items))
            try:
                new = plan_json(old, [op for _, op in items])
            except Unsafe as exc:
                self.snippets.append(
                    Snippet(str(path), _json_snippet_for(items), f"{path.name} not edited: {exc}")
                )
                continue
            if new is None:
                self.already += [f"{label} ({path})" for label in labels]
                continue
            if text is not None and text.text and not text.text.endswith("\n"):
                new = new.rstrip("\n")
            enc = text or Text("")
            if load_json(old)[2]:
                labels.append("re-indents lines it does not change: read the whole diff")
            edits.append(FileEdit(path, labels, raw, enc.encode(new), old or "", new))
        for path, items in self.toml_ops.items():
            raw = _read_bytes(path)
            text = Text.decode(raw)
            current = text.text if text else None
            labels = []
            failed = False
            for label, table, values in items:
                try:
                    new = plan_toml(current, table, values)
                except Unsafe as exc:
                    self.snippets.append(
                        Snippet(str(path), toml_table_snippet(table, values), f"{path.name} not edited: {exc}")
                    )
                    failed = True
                    break
                if new is None:
                    self.already.append(f"{label} ({path})")
                else:
                    current = new
                    labels.append(label)
            if failed or not labels:
                continue
            enc = text or Text("")
            assert current is not None
            edits.append(FileEdit(path, labels, raw, enc.encode(current), text.text if text else "", current))
        for path, (label, data) in self.raw_files.items():
            raw = _read_bytes(path)
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
    if is_toml(host):
        planner.add_toml(path, "registration", entry_path(host), values)
    else:
        for key, value in values.items():
            planner.add_json(path, "registration", SetKey(entry_path(host) + tuple(key.split(".")), value))
    return path


def plan_side_files(host: Host, roots: Roots, opts: Options, planner: Planner) -> None:
    timeout = host.timeout
    if timeout.key and timeout.file and timeout.needs_raising() and usable(timeout.source):
        planner.add_json(roots.path(timeout.file), "tool-call limit", SetKey(tuple(timeout.key.split(".")), timeout.wanted_value()))
    approval = host.approval
    if approval and approval.kind == "list" and (approval.by_default or opts.approve):
        if approval.writable and approval.file and usable(approval.source):
            planner.add_json(roots.path(approval.file), "approval rule", AddToList(tuple(approval.key.split(".")), approval.value))
        else:
            planner.snippets.append(
                Snippet(roots.path(approval.file).as_posix() if approval.file else host.label, approval.snippet, "the approval rule is not written by the installer")
            )


def plan_status_line(host: Host, roots: Roots, opts: Options, planner: Planner) -> None:
    spec = host.status_line
    if spec is None or not opts.status_line or opts.scope != "user":
        return
    status_cmd = status_command(opts.command, opts.args)
    path = roots.path(spec.file)
    if not spec.writable or not usable(spec.source):
        planner.snippets.append(
            Snippet(str(path), spec.snippet.replace("<status command>", status_cmd.replace('"', '\\"')), "the status line is not written by the installer")
        )
        return
    text = Text.decode(_read_bytes(path))
    try:
        data, _, _ = load_json(text.text if text else None)
    except Unsafe as exc:
        planner.snippets.append(Snippet(str(path), json.dumps({spec.key: _status_value(spec, status_cmd)}, indent=2), f"{path.name} not edited: {exc}"))
        return
    existing = get_path(data, spec.key.split("."))
    if existing is None:
        planner.add_json(path, "status line", SetKey(tuple(spec.key.split(".")), _status_value(spec, status_cmd)))
        return
    existing_cmd = existing.get("command", "") if isinstance(existing, dict) else str(existing)
    if "agent_bridge" in existing_cmd and "status" in existing_cmd or "agent-bridge status" in existing_cmd:
        planner.already.append(f"status line ({path})")
        return
    planner.snippets.append(
        Snippet(
            existing_cmd or str(path),
            user_script_line(existing_cmd, status_cmd) + "\n",
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
    path = roots.path(template) / SKILL_NAME / "SKILL.md"
    if path in seen:
        return
    seen.add(path)
    planner.raw_files[path] = ("usage skill", packaged_skill())


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
        runner: Runner | None,
        path_lookup: PathLookup,
    ) -> None:
        self.opts = opts
        self.roots = roots
        self.out = out
        self.ask = ask
        self.runner = runner
        self.path_lookup = path_lookup
        self.skill_paths: set[Path] = set()
        self.written: set[Path] = set()

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
        if edit.raw_old is None and edit.labels == ["usage skill"]:
            self.out(f"(the packaged {SKILL_NAME}/SKILL.md, {len(edit.new_text.splitlines())} lines)")
            return
        diff = difflib.unified_diff(
            edit.old_text.splitlines(keepends=True),
            edit.new_text.splitlines(keepends=True),
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
        if _read_bytes(edit.path) != edit.raw_old:
            report.failed.append(f"{label}: {edit.path} changed while the diff was shown; run install again")
            return
        if edit.raw_old is not None:
            backup = backup_path(edit.path)
            _write_bytes(backup, edit.raw_old)
            self.out(f"backup: {backup}")
        _write_bytes(edit.path, edit.new_bytes)
        self.written.add(edit.path)
        report.done.append(f"{label} ({edit.path})")

    def official_command(self, host: Host, path: Path | None, report: HostReport) -> None:
        """Run the host's own `mcp add` when the bridge is not registered yet and running is allowed."""
        reg = host.register
        if reg is None or not usable(reg.source) or self.opts.scope not in reg.scopes or path is None:
            return
        if self.opts.scope != "user":
            return  # a project-scope `mcp add` writes into the current folder, not --project: edit the file
        try:
            if registered_entry(host, path) is not None:
                return
        except Unsafe:
            return
        binary = self.path_lookup(reg.argv[0])
        if not binary or hosts_mod.is_shim(binary):
            return
        argv = [binary, *expand_register(reg.argv, self.opts)[1:]]
        shown = " ".join(expand_register(reg.argv, self.opts))
        if self.runner is None:
            return  # --home without an injected runner: the host's command would write the real home
        if self.opts.print_only:
            self.out(f"would run: {shown}")
            self.out("(the diff below shows the file as it would be after that command and the edits that follow it)")
            return
        self.out(f"run: {shown}")
        if not self.confirm("Run it?"):
            report.declined.append(f"registration command ({shown})")
            return
        try:
            result = self.runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            report.failed.append(f"registration command failed to start: {exc}; editing the file instead")
            return
        if result.returncode != 0:
            first = (result.stderr or result.stdout or "").strip().splitlines()[:1]
            report.failed.append(
                f"registration command exited {result.returncode}"
                + (f": {first[0]}" if first else "")
                + "; editing the file instead"
            )
            return
        report.done.append(f"registration via `{shown}`")

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

        path, why_not = config_path(host, self.roots, opts.scope)
        if why_not is None:  # an unverified file cannot be checked for an existing registration first
            self.official_command(host, path, report)

        planner = Planner(self.roots, opts)
        plan_registration(host, self.roots, opts, planner)
        plan_side_files(host, self.roots, opts, planner)
        plan_status_line(host, self.roots, opts, planner)
        plan_skill(host, self.roots, opts, planner, self.skill_paths)
        for edit in planner.edits():
            self.apply(edit, report)
        report.already += planner.already
        for snippet in planner.snippets:
            self.out(f">> paste by hand into {snippet.where}: {snippet.reason}")
            self.out(snippet.text.rstrip("\n"))
            report.printed.append(f"snippet for {snippet.where}")
        if host.register and host.name == "vscode-copilot" and opts.scope == "user":
            self.out("or run: " + " ".join(expand_register(host.register.argv, opts)))
        return report


def _split_command(raw: str) -> list[str]:
    import shlex

    if os.name == "nt":
        parts = shlex.split(raw, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(raw)


def default_command() -> tuple[str, list[str]]:
    """The command that starts this bridge: the running interpreter with `-m agent_bridge`."""
    return os.path.abspath(sys.executable), ["-m", "agent_bridge"]


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
    parser.add_argument("--no-skill", dest="skill", action="store_false", help="do not offer the usage skill")
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
    """`agent-bridge install`. The host's own `mcp add` runs only without --home and --print, or with an injected runner."""
    args = _install_parser().parse_args(argv)
    env = dict(os.environ) if env is None else dict(env)
    if args.command:
        parts = _split_command(args.command)
        if not parts:
            out("agent-bridge install: --command is empty")
            return 2
        command, cmd_args = parts[0], parts[1:]
    else:
        command, cmd_args = default_command()
    if args.home is not None:
        roots = Roots.for_home(args.home, project=args.project)
    else:
        roots = Roots.from_env(env, project=args.project)
        if runner is None:
            runner = _real_runner
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
    session = Session(opts, roots, out, ask, runner, path_lookup)
    out(f"command: {' '.join([command, *cmd_args])}")
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


def _real_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=120, check=False)


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
    if isinstance(command, list):
        return command[0] if command else None
    return command if isinstance(command, str) else None


def check_host(host: Host, roots: Roots, path_lookup: PathLookup, skill: bytes) -> list[Check]:
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
        skill_path = roots.path(host.skills_user) / SKILL_NAME / "SKILL.md"
        current = _read_bytes(skill_path)
        if current is None:
            checks.append(Check(f"{label}: skill", "warn", f"missing at {skill_path}", fix))
        elif current.replace(b"\r\n", b"\n") != skill.replace(b"\r\n", b"\n"):
            checks.append(Check(f"{label}: skill", "warn", f"out of date at {skill_path}", fix))
        else:
            checks.append(Check(f"{label}: skill", "ok", str(skill_path)))
    return checks


def check_state(state_dir: Path) -> list[Check]:
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
        # immutable=1 with mode=ro: no lock, no -wal or -shm file is made beside the database.
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
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
    skill = packaged_skill()
    detected = hosts_mod.detect_installed(env, path_lookup, roots=roots, exists=_exists)
    if not detected:
        checks.append(Check("hosts", "warn", "no host found on this machine", "agent-bridge install --host NAME"))
    for host in detected:
        checks += check_host(host, roots, path_lookup, skill)
    checks += check_state(state_dir)
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
