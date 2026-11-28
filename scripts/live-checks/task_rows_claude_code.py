"""Does Claude Code show a bridge job as a background task row? Drives one headless Claude Code session.

Run from the checkout:  uv run python scripts/live-checks/task_rows_claude_code.py [backend] [seconds to hold]
Starts `claude -p` in streaming mode with this checkout's bridge as its only MCP server (temp state folder,
every line on the wire copied to a log), asks it to call `run` once, and reads the wire log.

Passes when the bridge answered `run` with a task, Claude Code polled it with `tasks/get`, and the session's
last answer holds the word in the note. Spends a little of the Claude plan (Haiku) and of the backend's quota.
A one-shot `claude -p` gets no tasks: the streaming input format and CLAUDE_AUTO_BACKGROUND_TASKS=1 are needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from _bridge import NESTING_KEYS, REPO, temp_dir

HERE = Path(__file__).resolve().parent
WORD = "heliotrope"
JOBS = {
    "codex": 'backend="codex", effort="low"',
    "neon": 'backend="neon", model="flash"',
    "claude": 'backend="claude", model="haiku", effort="low"',
}


def main() -> int:
    backend = sys.argv[1] if len(sys.argv) > 1 else "codex"
    hold = int(sys.argv[2]) if len(sys.argv) > 2 else 240
    if backend not in JOBS:
        sys.exit(f"unknown backend {backend!r}; choose from {list(JOBS)}")

    state = temp_dir("bridge-live-state-")
    work = temp_dir("bridge-live-cwd-")
    logs = temp_dir("bridge-live-logs-")
    wire = logs / "wire.log"
    (work / "note.txt").write_text(f"The secret word is {WORD}.\n", encoding="utf-8")
    config = {
        "mcpServers": {
            "agent-bridge": {
                "command": sys.executable,
                "args": [str(HERE / "tee.py"), sys.executable, "-m", "agent_bridge"],
                "env": {"AGENT_BRIDGE_STATE_DIR": str(state), "PROBE_WIRE_LOG": str(wire), "PYTHONPATH": str(REPO / "src")},
            }
        }
    }
    (logs / "mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    # A fresh host window: nothing that marks this process as a Claude Code session or a bridge job.
    env = dict(os.environ)
    for key in list(env):
        if key == "CLAUDECODE" or key.startswith("CLAUDE_CODE_") or key in {"CLAUDE_PID", "CLAUDE_EFFORT"}:
            env.pop(key)
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", *NESTING_KEYS):
        env.pop(key, None)
    env["CLAUDE_AUTO_BACKGROUND_TASKS"] = "1"
    env["MCP_PROTOCOL_NEGOTIATION"] = "auto"

    cmd = [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--model", "haiku", "--mcp-config", str(logs / "mcp.json"), "--strict-mcp-config",
        "--allowedTools", "mcp__agent-bridge__run", "mcp__agent-bridge__jobs", "TaskStop",
        "--permission-mode", "dontAsk",
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=work, env=env,
        shell=(os.name == "nt"),
    )
    events: list[dict] = []

    def read_out() -> None:
        for raw in iter(proc.stdout.readline, b""):
            try:
                events.append(json.loads(raw))
            except ValueError:
                events.append({"type": "raw", "text": raw.decode("utf-8", "replace")[:300]})

    threading.Thread(target=read_out, daemon=True).start()
    prompt = (
        f"Call the run tool of agent-bridge exactly once with {JOBS[backend]}, mode=\"read_only\", "
        f"label=\"live-check\", cwd={json.dumps(str(work))} and "
        "prompt=\"Read note.txt in the current folder and reply with only the secret word it names.\". "
        "If it comes back as a background task, wait for it to finish. If it comes back as a receipt, collect it "
        "with the jobs tool. Then tell me, in one line, exactly what the job's response was."
    )
    message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
    proc.stdin.write((json.dumps(message) + "\n").encode())
    proc.stdin.flush()

    deadline = time.time() + hold
    while time.time() < deadline and proc.poll() is None:
        time.sleep(1)
        results = [event for event in events if event.get("type") == "result"]
        # With a task row there are two results: the turn that started the job and the turn the result woke.
        if len(results) >= 2 or (results and WORD in str(results[-1].get("result")).lower()):
            break
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()

    text = wire.read_text(encoding="utf-8", errors="replace") if wire.exists() else ""
    lines = text.splitlines()
    client = next((line for line in lines if '"clientInfo"' in line), "")
    client_name = ""
    if client:
        try:
            client_name = json.loads(client.split(" ", 2)[2])["params"]["clientInfo"]["name"]
        except (ValueError, KeyError, IndexError):
            pass
    as_task = sum(1 for line in lines if " S>C " in line and '"resultType":"task"' in line.replace(" ", ""))
    polls = sum(1 for line in lines if " C>S " in line and '"tasks/get"' in line)
    cancels = sum(1 for line in lines if " C>S " in line and '"tasks/cancel"' in line)
    answers = [str(event.get("result")) for event in events if event.get("type") == "result"]
    told = bool(answers) and WORD in answers[-1].lower()

    print(f"client name on the wire: {client_name or 'not seen'}")
    print(f"run answered as a task: {as_task}   tasks/get polls: {polls}   tasks/cancel: {cancels}")
    print(f"turns that ended: {len(answers)}   last answer: {answers[-1][:200] if answers else 'none'}")
    for event in events:
        if event.get("type") == "system" and event.get("subtype") not in {"init", None}:
            extra = {k: v for k, v in event.items() if k not in {"type", "uuid", "session_id"}}
            print("[system]", json.dumps(extra)[:400])
    ok = as_task >= 1 and polls >= 1 and told
    print("PASS" if ok else "FAIL", "- wire log:", wire, "- state folder:", state)
    if not ok:
        err = proc.stderr.read().decode("utf-8", "replace")
        if err.strip():
            print("STDERR:", err[-1200:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
