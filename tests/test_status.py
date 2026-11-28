"""`agent-bridge status` and the one status line: formatting, modes, and that it never writes."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from agent_bridge import nesting, status_cmd
from agent_bridge.config import Settings, state_dir_from_env
from agent_bridge.status_text import format_job_line, format_summary_line, sanitize

NOW = 1_800_000_000.0
SRC = str(Path(__file__).resolve().parents[1] / "src")


# --- helpers ------------------------------------------------------------------------------------


def _job(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "codex-1",
        "backend": "codex",
        "model": "gpt-6.2-sol",
        "requested_model": "gpt-6.2-sol",
        "effort": "xhigh",
        "status": "running",
        "created_at": NOW - 200,
        "started_at": NOW - 130,
        "last_activity": "reading jobs.py",
    }
    base.update(kw)
    return base


def _store(state_dir: Path):
    from agent_bridge.storage import BridgeStore  # the test may build with the real store

    return BridgeStore(state_dir)


def _add(store, *, owner: str = "owner-a", **kw: Any) -> str:
    from agent_bridge.models import JobRecord

    now = time.time()
    fields: dict[str, Any] = {
        "job_id": "codex-aaa",
        "owner_id": owner,
        "backend": "codex",
        "cwd": "C:/work/repo",
        "mode": "read_only",
        "requested_model": "gpt-6.2-sol",
        "model": "gpt-6.2-sol",
        "prompt_preview": "do it",
        "effort": "xhigh",
        "status": "running",
        "created_at": now - 140,
        "started_at": now - 130,
        "last_activity": "reading jobs.py",
    }
    fields.update(kw)
    store.insert_job(JobRecord(**fields))
    return fields["job_id"]


def _live_owner(store, owner: str = "owner-a") -> None:
    store.register_owner(owner, os.getpid())


def _run(capsys: pytest.CaptureFixture[str], state_dir: Path, *args: str) -> tuple[int, str, str]:
    rc = status_cmd.run([*args, "--state-dir", str(state_dir)])
    out = capsys.readouterr()
    return rc, out.out, out.err


def _env(state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = str(state_dir)
    env["PYTHONPATH"] = SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    for name in ("COLUMNS", "PYTHONIOENCODING", "PYTHONUTF8"):  # as a status-line host would run it
        env.pop(name, None)
    return env


@pytest.fixture(autouse=True)
def _no_columns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    # Never fall through to the real ~/.agent-bridge, even by mistake.
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(tmp_path / "unused-state"))


# --- the line -----------------------------------------------------------------------------------


def test_line_parts_in_order_with_unknown_parts_left_out() -> None:
    assert format_job_line(_job(), now=NOW) == (
        "codex · requested gpt-6.2-sol · xhigh · running 2m10s · reading jobs.py"
    )
    bare = format_job_line(_job(backend="neon", model=None, requested_model=None, effort=None,
                                last_activity=None), now=NOW)
    assert bare == "neon · running 2m10s"
    assert "None" not in bare
    long = format_job_line(_job(label="review auth", quota_pool="codex"), now=NOW, long=True)
    assert long.endswith(" · reading jobs.py · review auth · codex")


def test_codex_model_is_labelled_requested_and_its_default_named() -> None:
    assert "requested gpt-6.2-sol" in format_job_line(_job(), now=NOW)
    for model in ("codex-default", None):
        line = format_job_line(_job(model=model, requested_model=None), now=NOW)
        assert line.startswith("codex · Codex's default model · xhigh")


def test_neon_and_claude_models_are_shown_plain() -> None:
    neon = format_job_line(_job(backend="neon", model="gemini-4-neon-high", effort="high"), now=NOW)
    assert neon.startswith("neon · gemini-4-neon-high · high")
    claude = format_job_line(_job(backend="claude", model="claude-sonnet-5"), now=NOW)
    assert claude.startswith("claude · claude-sonnet-5 ·")
    assert "requested" not in neon + claude


def test_legacy_claude_default_model_without_source_is_preserved() -> None:
    line = format_job_line(
        _job(backend="claude", model="claude-default", requested_model=None, model_source=None),
        now=NOW,
    )
    assert line == "claude · claude-default · xhigh · running 2m10s · reading jobs.py"
    neon_line = format_job_line(
        _job(backend="neon", model="neon-default", requested_model=None, model_source=None),
        now=NOW,
    )
    assert neon_line == "neon · neon-default · xhigh · running 2m10s · reading jobs.py"
    explicit_default = format_job_line(
        _job(backend="claude", model="claude-default", requested_model=None, model_source="default"),
        now=NOW,
    )
    assert explicit_default == "claude · Claude's default model · xhigh · running 2m10s · reading jobs.py"


def test_model_source_overrides_the_backend_rule() -> None:
    assert format_job_line(_job(model_source="observed"), now=NOW).startswith("codex · gpt-6.2-sol ·")
    assert "requested claude-x" in format_job_line(
        _job(backend="claude", model="claude-x", model_source="requested"), now=NOW
    )
    assert "Claude's default model" in format_job_line(
        _job(backend="claude", model="claude-x", model_source="default"), now=NOW
    )


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"status": "queued", "started_at": None, "created_at": NOW - 12}, "queued 12s"),
        ({"started_at": NOW - 130}, "running 2m10s"),
        ({"started_at": NOW - 3780}, "running 1h03m"),
        ({"status": "completed", "duration_seconds": 182.4, "started_at": NOW - 999}, "completed 3m02s"),
        ({"status": "failed", "duration_seconds": 5.0}, "failed 5s"),
    ],
)
def test_elapsed_time_and_status(job: dict[str, Any], expected: str) -> None:
    assert f" · {expected} · " in format_job_line(_job(**job), now=NOW)


def test_activity_is_sanitised_so_it_cannot_move_the_cursor_or_forge_a_line() -> None:
    evil = (
        "rm -rf\x1b[2J\x1b[H x\nbridge: codex · fake second line\r\x1b]0;title\x07"
        "\x9b31m\u202eevil\u2028tail\x00\t end"
    )
    line = format_job_line(_job(last_activity=evil), now=NOW)
    assert not any(ch in line for ch in "\x1b\n\r\x07\x9b\u202e\u2028\x00\t")
    assert "[2J" not in line and "]0;" not in line
    assert line.count("\n") == 0
    assert "rm -rf x bridge: codex · fake second" in line  # whitespace collapsed, one line
    assert sanitize("a\x1b[31mred\x1b[0m b") == "a red b"


def test_every_part_is_sanitised_not_only_activity() -> None:
    line = format_job_line(
        _job(backend="neon", model="m\x1b[2Jx\ny", effort="hi\rgh", label="l\x1b]8;;u\x07z"),
        now=NOW,
        long=True,
    )
    assert "\x1b" not in line and "\n" not in line and "\r" not in line
    assert "m x y" in line


def test_long_activity_is_truncated_with_an_ellipsis() -> None:
    line = format_job_line(_job(last_activity="x" * 500), now=NOW)
    assert line.endswith("…")
    assert len(line) < 140


def test_stale_activity_shows_its_age() -> None:
    assert format_job_line(_job(last_activity_at=NOW - 45), now=NOW).endswith("reading jobs.py (45s ago)")
    assert format_job_line(_job(last_activity_at=NOW - 5), now=NOW).endswith("reading jobs.py")


def test_width_truncates_the_whole_line() -> None:
    line = format_job_line(_job(), now=NOW, width=30)
    assert 25 < len(line) <= 30 and line.endswith("…")
    assert format_job_line(_job(), now=NOW, width=1000) == format_job_line(_job(), now=NOW)


def test_summary_line_for_zero_one_and_several_jobs() -> None:
    assert format_summary_line([], now=NOW, prefix="bridge: ") == ""
    one = format_summary_line([_job()], now=NOW, prefix="bridge: ")
    assert one == "bridge: codex · requested gpt-6.2-sol · xhigh · running 2m10s · reading jobs.py"
    two = format_summary_line(
        [_job(), _job(backend="neon", model="gemini-4-neon-high", started_at=NOW - 40)],
        now=NOW,
        prefix="bridge: ",
    )
    assert two == "bridge: 2 jobs: codex · requested gpt-6.2-sol · 2m10s | neon · gemini-4-neon-high · 40s"


# --- the command: modes -------------------------------------------------------------------------


def test_line_mode_with_zero_one_and_several_jobs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    assert _run(capsys, state, "--line") == (0, "", "")

    _add(store)
    rc, out, _ = _run(capsys, state, "--line")
    assert rc == 0
    assert out.startswith("bridge: codex · requested gpt-6.2-sol · xhigh · running 2m1")
    assert out.endswith(" · reading jobs.py\n")

    _add(store, job_id="neon-bbb", backend="neon", model="gemini-4-neon-high", requested_model="neon-high")
    rc, out, _ = _run(capsys, state, "--line")
    assert out.startswith("bridge: 2 jobs: codex · requested gpt-6.2-sol · 2m1")
    assert "| neon · gemini-4-neon-high · 2m1" in out

    rc, out, _ = _run(capsys, state, "--line", "--width", "25", "--prefix", ">> ")
    assert out == ">> 2 jobs: codex · reque…\n"


def test_line_width_defaults_to_columns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    _add(store)
    rc, out, _ = _run(capsys, state, "--line", "--prefix", "p" * 100 + " ")
    assert 110 < len(out.rstrip("\n")) <= 120 and out.rstrip("\n").endswith("…")
    monkeypatch.setenv("COLUMNS", "40")
    rc, out, _ = _run(capsys, state, "--line")
    assert 35 < len(out.rstrip("\n")) <= 40


def test_lines_mode_prints_one_line_per_running_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    _add(store)
    _add(store, job_id="claude-c", backend="claude", model="claude-sonnet-5")
    _add(store, job_id="codex-old", status="completed", finished_at=time.time() - 5, duration_seconds=9.0)
    rc, out, _ = _run(capsys, state, "--lines")
    lines = out.splitlines()
    assert rc == 0 and len(lines) == 2
    assert lines[0].startswith("bridge: codex · requested gpt-6.2-sol")
    assert lines[1].startswith("bridge: claude · claude-sonnet-5")


def test_table_lists_running_then_recent_and_recent_zero_hides_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    now = time.time()
    _add(store, label="review auth")
    _add(store, job_id="neon-done", backend="neon", model="gemini-4-neon-high", status="completed",
         finished_at=now - 60, duration_seconds=42.0)
    _add(store, job_id="neon-ancient", backend="neon", status="failed", created_at=now - 9000,
         finished_at=now - 8000, duration_seconds=3.0)

    rc, out, _ = _run(capsys, state)
    assert rc == 0
    assert out.index("Running (1)") < out.index("codex-aaa") < out.index("Finished in the last 10 minutes (1)")
    assert "neon-done" in out and "completed 42s" in out and "review auth" in out
    assert "JOB" in out and "POOL" in out and "neon-ancient" not in out

    rc, out, _ = _run(capsys, state, "--recent", "0")
    assert "codex-aaa" in out and "neon-done" not in out and "Finished" not in out

    rc, out, _ = _run(capsys, state, "--all")
    assert "neon-ancient" in out and "neon-done" in out


def test_finished_rows_are_capped_and_the_cap_is_shown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    now = time.time()
    for i in range(25):
        _add(store, job_id=f"done-{i:02d}", status="completed", finished_at=now - 100 + i, duration_seconds=1.0)
    rc, out, _ = _run(capsys, state)
    assert "Finished in the last 10 minutes (20 of 25, --limit to show more)" in out
    assert "done-24" in out and "done-04" not in out  # the newest first
    rc, out, _ = _run(capsys, state, "--limit", "30")
    assert "Finished in the last 10 minutes (25)" in out
    rc, out, _ = _run(capsys, state, "--json")
    data = json.loads(out)
    assert len(data["finished"]) == 20 and data["finished_total"] == 25


def test_json_mode_is_one_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    _add(store, last_activity="ls\x1b[2J\nnext")
    rc, out, _ = _run(capsys, state, "--json")
    data = json.loads(out)
    assert rc == 0 and data["exists"] is True
    (job,) = data["active"]
    assert job["job_id"] == "codex-aaa"
    assert job["model_display"] == "requested gpt-6.2-sol"
    assert job["display_status"] == "running"
    assert job["last_activity"] == "ls next"
    assert job["line"].startswith("codex · requested gpt-6.2-sol · xhigh · running")
    assert job["elapsed_seconds"] >= 129


def test_cwd_and_parent_run_filters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    repo_a, repo_b = tmp_path / "RepoA", tmp_path / "repo-b"
    _add(store, job_id="a-1", cwd=str(repo_a), parent_run="sweep")
    _add(store, job_id="b-1", cwd=str(repo_b))

    rc, out, _ = _run(capsys, state, "--json", "--cwd", str(repo_a) + os.sep)
    assert [j["job_id"] for j in json.loads(out)["active"]] == ["a-1"]
    if sys.platform == "win32":
        rc, out, _ = _run(capsys, state, "--json", "--cwd", str(repo_a).lower())
        assert [j["job_id"] for j in json.loads(out)["active"]] == ["a-1"]
    rc, out, _ = _run(capsys, state, "--json", "--parent-run", "sweep")
    assert [j["job_id"] for j in json.loads(out)["active"]] == ["a-1"]
    rc, out, _ = _run(capsys, state, "--json", "--parent-run", "nope")
    assert json.loads(out)["active"] == []


def test_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "never"
    assert _run(capsys, state, "--line") == (0, "", "")
    assert _run(capsys, state, "--lines") == (0, "", "")
    rc, out, err = _run(capsys, state)
    assert rc == 0 and err == ""
    assert out.count("\n") == 1 and out.startswith("No bridge database at ")
    rc, out, _ = _run(capsys, state, "--json")
    assert json.loads(out)["exists"] is False
    assert not state.exists()  # nothing was created


@pytest.mark.parametrize("mode", [(), ("--json",)])
def test_missing_state_path_is_sanitised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mode: tuple[str, ...]
) -> None:
    state = tmp_path / "odd\x1b[2J\u202epath"
    rc, out, err = _run(capsys, state, *mode)
    assert rc == 0 and err == ""
    path = sanitize(state / "bridge.db")
    if mode:
        payload = json.loads(out)
        assert payload["database"] == path
        out = payload["message"]
    assert path in out
    assert all(control not in out for control in ("\x1b", "\u202e", "[2J"))


def test_snapshot_error_diagnostic_is_sanitised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise status_cmd.SnapshotError("corrupt\x1b[2J\nheader\x07")

    monkeypatch.setattr(status_cmd, "read_snapshot", broken)
    state = tmp_path / "odd\x1b[2J\u202epath"
    rc, out, err = _run(capsys, state, "--line")
    assert rc == 1 and out == ""
    assert sanitize(state / "bridge.db") in err and "corrupt header" in err
    assert err.count("\n") == 1 and "\x1b" not in err


def test_argument_error_diagnostic_is_sanitised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(capsys, tmp_path, "--unexpected=\x1b[2J\u202eforged")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --unexpected= forged" in err
    assert "\x1b" not in err and "\u202e" not in err


def test_database_lacking_optional_columns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "old"
    state.mkdir()
    conn = sqlite3.connect(state / "bridge.db")
    conn.execute("CREATE TABLE jobs (job_id TEXT, backend TEXT, status TEXT, created_at REAL, model TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('neon-1', 'neon', 'running', ?, 'gemini-x')", (time.time() - 3,))
    conn.execute("INSERT INTO jobs VALUES ('neon-2', 'neon', 'completed', ?, 'gemini-x')", (time.time() - 30,))
    conn.commit()
    conn.close()
    rc, out, err = _run(capsys, state, "--line")
    assert rc == 0 and err == ""
    assert out.startswith("bridge: neon · gemini-x · running ")  # no owners table: assumed live
    rc, out, _ = _run(capsys, state)
    assert rc == 0 and "neon-1" in out and "neon-2" in out
    rc, out, _ = _run(capsys, state, "--json", "--parent-run", "x")
    assert json.loads(out)["active"] == []


def test_legacy_owner_without_identity_column_uses_pid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = sqlite3.connect(tmp_path / "bridge.db")
    try:
        conn.execute("CREATE TABLE owners (owner_id TEXT, pid INTEGER)")
        conn.execute("CREATE TABLE jobs (job_id TEXT, owner_id TEXT, status TEXT)")
        conn.execute("INSERT INTO owners VALUES ('legacy', ?)", (os.getpid(),))
        conn.execute("INSERT INTO jobs VALUES ('job', 'legacy', 'running')")
        conn.commit()
    finally:
        conn.close()
    assert _run(capsys, tmp_path, "--lines") == (0, "bridge: running\n", "")


def test_unreadable_database_is_one_sentence_on_stderr_and_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "bad"
    state.mkdir()
    (state / "bridge.db").write_bytes(b"this is not a database file at all" * 64)
    before = (state / "bridge.db").read_bytes()
    for mode in ([], ["--line"], ["--json"]):
        rc, out, err = _run(capsys, state, *mode)
        assert rc == 1 and out == ""
        assert err.startswith("agent-bridge status: cannot read ") and err.count("\n") == 1
    assert (state / "bridge.db").read_bytes() == before


def test_job_of_a_closed_window_is_shown_as_window_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    store = _store(state)
    store.register_owner("gone", os.getpid())
    store.mark_owner_exited("gone")
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)
    store.register_owner("dead-pid", int(dead.stdout))
    _live_owner(store, "alive")
    _add(store, job_id="exited-1", owner="gone")
    _add(store, job_id="dead-1", owner="dead-pid", status="queued", started_at=None)
    _add(store, job_id="orphan-1", owner="no-such-owner")
    _add(store, job_id="live-1", owner="alive")

    rc, out, _ = _run(capsys, state, "--json")
    shown = {j["job_id"]: j["display_status"] for j in json.loads(out)["active"]}
    assert shown == {"exited-1": "window closed", "dead-1": "window closed",
                     "orphan-1": "window closed", "live-1": "running"}
    rc, out, _ = _run(capsys, state)
    assert out.count("window closed") == 3
    rc, out, _ = _run(capsys, state, "--line")
    assert out.startswith("bridge: codex ·") and "jobs:" not in out  # only the live one
    with sqlite3.connect(state / "bridge.db") as conn:
        assert {r[0] for r in conn.execute("SELECT status FROM jobs")} == {"running", "queued"}


def test_reused_owner_pid_is_shown_as_window_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pid = os.getpid()
    current = nesting.process_identity(pid)
    if current is None:
        pytest.skip("the platform does not expose a process identity")
    family, ticks = current.rsplit(":", 1)
    store = _store(tmp_path)
    store.register_owner("old", pid, process_identity=f"{family}:{int(ticks) + 1}")
    store.register_owner("current", pid, process_identity=current)
    _add(store, job_id="stale", owner="old")
    _add(store, job_id="live", owner="current")
    rc, out, err = _run(capsys, tmp_path, "--json")
    assert rc == 0 and err == ""
    shown = {job["job_id"]: job["display_status"] for job in json.loads(out)["active"]}
    assert shown == {"stale": "window closed", "live": "running"}
    rc, out, err = _run(capsys, tmp_path, "--lines")
    assert rc == 0 and err == "" and len(out.splitlines()) == 1


def test_unreadable_owner_process_identity_keeps_live_job_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_bridge.storage import _is_owner_row_alive

    pid = os.getpid()
    store = _store(tmp_path)
    store.register_owner("elevated", pid, process_identity="win:123456789")
    _add(store, job_id="live", owner="elevated")

    monkeypatch.setattr(nesting, "process_identity", lambda _pid: None)
    assert _is_owner_row_alive(
        pid, None, "win:123456789", is_pid_alive_fn=status_cmd._pid_alive, process_identity_fn=nesting.process_identity,
    ) is True

    rc, out, err = _run(capsys, tmp_path, "--json")
    assert rc == 0 and err == ""
    (job,) = json.loads(out)["active"]
    assert job["display_status"] == "running"
    assert job["window_closed"] is False

    rc, out, err = _run(capsys, tmp_path, "--line")
    assert rc == 0 and err == ""
    assert out.startswith("bridge: codex · ")

    # A readable identity that differs still marks the window closed.
    monkeypatch.setattr(nesting, "process_identity", lambda _pid: "win:999999999")
    rc, out, err = _run(capsys, tmp_path, "--json")
    assert rc == 0 and err == ""
    (job,) = json.loads(out)["active"]
    assert job["display_status"] == "window closed"
    assert job["window_closed"] is True


@pytest.mark.parametrize("mode", ["--line", "--lines"])
@pytest.mark.parametrize("all_jobs", [False, True], ids=["default", "all"])
def test_line_modes_query_only_active_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    mode: str, all_jobs: bool,
) -> None:
    store = _store(tmp_path)
    _live_owner(store)
    _add(store)
    _add(store, job_id="finished", status="completed", finished_at=time.time() - 1)
    queries: list[str] = []
    connect = sqlite3.connect

    def trace_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", trace_connection)
    rc, out, err = _run(capsys, tmp_path, mode, *(["--all"] if all_jobs else []))
    assert rc == 0 and err == "" and len(out.splitlines()) == 1
    job_queries = [query for query in queries if "FROM jobs j" in query]
    assert len(job_queries) == 1
    assert "WHERE j.status IN ('queued', 'running') ORDER BY" in job_queries[0]


def test_watch_appends_when_not_a_terminal_and_stops_on_ctrl_c(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    store = _store(state)
    _live_owner(store)
    _add(store)
    frames = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        frames["n"] += 1
        if frames["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(status_cmd.time, "sleep", fake_sleep)
    rc, out, _ = _run(capsys, state, "--watch", "0.5")
    assert rc == 0
    assert out.count("Running (1)") == 2
    assert "\x1b" not in out  # no clear-screen when stdout is not a terminal


def test_state_dir_rule_is_shared_with_the_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "from-env"
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(state))
    assert state_dir_from_env() == state == Settings.from_env().state_dir
    monkeypatch.setenv("AGENT_BRIDGE_CLAUDE_AUTH", "nonsense")  # Settings would raise; the helper does not
    assert state_dir_from_env() == state
    monkeypatch.delenv("AGENT_BRIDGE_STATE_DIR")
    assert state_dir_from_env() == Path.home() / ".agent-bridge"

    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(state))
    store = _store(state)
    _live_owner(store)
    _add(store)
    assert status_cmd.run(["--line"]) == 0
    assert capsys.readouterr().out.startswith("bridge: codex ·")


# --- the command: never writes ------------------------------------------------------------------


def test_no_mode_writes_to_the_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    store = _store(state)
    store.register_owner("gone", os.getpid())
    store.mark_owner_exited("gone")
    _live_owner(store)
    _add(store)
    _add(store, job_id="stale", owner="gone")
    _add(store, job_id="done", status="completed", finished_at=time.time() - 1, duration_seconds=2.0)
    db = state / "bridge.db"
    with sqlite3.connect(db) as conn:  # fold the WAL in, so the main file is the whole database
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    before = db.read_bytes()

    monkeypatch.setattr(status_cmd.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt))
    modes = [[], ["--line"], ["--lines"], ["--json"], ["--all"], ["--recent", "0"], ["--watch"],
             ["--cwd", str(tmp_path)], ["--parent-run", "x"], ["--json", "--all"]]
    for mode in modes:
        rc, _, err = _run(capsys, state, *mode)
        assert rc == 0, (mode, err)

    assert db.read_bytes() == before
    wal = state / "bridge.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True) as conn:
        owners = conn.execute("SELECT owner_id, exited_at IS NOT NULL FROM owners ORDER BY owner_id").fetchall()
        stale = conn.execute("SELECT status FROM jobs WHERE job_id = 'stale'").fetchone()
    conn.close()
    assert owners == [("gone", 1), ("owner-a", 0)]
    assert stale == ("running",)


_WRITER = textwrap.dedent(
    """
    import os, sqlite3, sys, time
    from pathlib import Path
    from agent_bridge.models import JobRecord
    from agent_bridge.storage import BridgeStore

    store = BridgeStore(Path(sys.argv[1]))
    # Like a live bridge with other connections open: commits stay in the WAL, not the main file.
    keeper = sqlite3.connect(sys.argv[1] + "/bridge.db")
    keeper.execute("SELECT count(*) FROM sqlite_master").fetchall()
    store.register_owner("writer", os.getpid())
    now = time.time()
    store.insert_job(JobRecord(job_id="codex-live", owner_id="writer", backend="codex", cwd=".",
        mode="read_only", requested_model="gpt-6.2-sol", model="gpt-6.2-sol", prompt_preview="p",
        effort="high", status="queued", created_at=now))
    print("ready", flush=True)
    held = None
    for line in sys.stdin:
        cmd = line.strip()
        if cmd.startswith("activity "):
            store.update_job_progress("codex-live", owner_id="writer", status="running",
                                      started_at=time.time(), last_activity=cmd[9:])
        elif cmd == "hold":  # an open write transaction, uncommitted
            held = sqlite3.connect(sys.argv[1] + "/bridge.db", isolation_level=None)
            held.execute("BEGIN IMMEDIATE")
            held.execute("UPDATE jobs SET last_activity = 'uncommitted' WHERE job_id = 'codex-live'")
        elif cmd == "release":
            held.execute("COMMIT"); held.close(); held = None
        elif cmd == "hammer":
            end = time.time() + 1.5
            i = 0
            while time.time() < end:
                i += 1
                store.update_job_progress("codex-live", owner_id="writer", last_activity=f"step {i}")
        elif cmd == "finish":
            c = sqlite3.connect(sys.argv[1] + "/bridge.db")
            c.execute("UPDATE jobs SET status='completed', finished_at=?, duration_seconds=7 "
                      "WHERE job_id='codex-live'", (time.time(),))
            c.commit(); c.close()
        print("ok", flush=True)
    """
)


def test_live_view_follows_a_real_writer_and_never_errors_on_a_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    script = tmp_path / "writer.py"
    script.write_text(_WRITER, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script), str(state)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=_env(state),
    )

    def send(cmd: str) -> None:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()
        assert proc.stdout.readline().strip() == "ok"

    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        rc, out, _ = _run(capsys, state, "--line")
        assert rc == 0 and " · queued " in out

        send("activity reading jobs.py")
        rc, out, _ = _run(capsys, state, "--line")
        assert " · running " in out and out.rstrip().endswith("reading jobs.py")

        send("hold")  # the writer holds the write lock: the reader still sees the last commit
        rc, out, err = _run(capsys, state, "--line")
        assert (rc, err) == (0, "") and "reading jobs.py" in out and "uncommitted" not in out
        send("release")
        rc, out, _ = _run(capsys, state, "--line")
        assert out.rstrip().endswith("uncommitted")

        assert proc.stdin is not None
        proc.stdin.write("hammer\n")
        proc.stdin.flush()
        seen: set[str] = set()
        deadline = time.time() + 1.2
        while time.time() < deadline:
            rc, out, err = _run(capsys, state, "--json")
            assert (rc, err) == (0, "")
            (job,) = json.loads(out)["active"]
            seen.add(job["last_activity"])
        assert proc.stdout.readline().strip() == "ok"
        assert len(seen) > 1  # it followed the writer as it went

        send("finish")
        rc, out, _ = _run(capsys, state, "--line")
        assert out == ""
        rc, out, _ = _run(capsys, state)
        assert "codex-live" in out and "completed 7s" in out
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait(timeout=20)


# --- the command: imports and start-up ----------------------------------------------------------


@pytest.mark.parametrize("has_database", [True, False], ids=["active", "empty"])
def test_command_imports_neither_store_jobs_server_nor_mcp(tmp_path: Path, has_database: bool) -> None:
    state = tmp_path / "state"
    if has_database:
        store = _store(state)
        pid = os.getpid()
        store.register_owner("owner-a", pid, process_identity=nesting.process_identity(pid))
        _add(store)
    probe = (
        "import sys\n"
        "from agent_bridge.cli import main\n"
        "rc = main(['status', '--line'])\n"
        "bad = sorted(m for m in sys.modules if m in {'agent_bridge.storage', 'agent_bridge.jobs', "
        "'agent_bridge.server', 'agent_bridge.process_control', 'asyncio'} or m == 'mcp' or m.startswith('mcp.'))\n"
        f"if {not has_database!r} and 'sqlite3' in sys.modules: bad.append('sqlite3')\n"
        "print('BAD=' + ','.join(bad))\n"
        "raise SystemExit(rc)\n"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, env=_env(state), timeout=60)
    out = done.stdout.decode("utf-8")  # strict: the line is UTF-8 even when piped on Windows
    assert done.returncode == 0, done.stderr
    assert out.rstrip().endswith("BAD=")
    if has_database:
        assert out.startswith("bridge: codex · requested gpt-6.2-sol")
    else:
        assert out.strip() == "BAD="
