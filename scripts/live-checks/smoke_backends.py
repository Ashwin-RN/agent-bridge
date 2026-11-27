"""One tiny read-only job on each backend, through the real CLIs, with a temp state folder.

Run from the checkout:  uv run python scripts/live-checks/smoke_backends.py [backend ...]
Passes when every job is `completed` and its reply is the word in the note. Spends a little quota on each
subscription it uses. Exit code 1 if any job did not pass.
"""

from __future__ import annotations

import asyncio
import sys

from _bridge import brief, reply_data, server_params, temp_dir
from mcp import ClientSession
from mcp.client.stdio import stdio_client

WORD = "heliotrope"
PROMPT = "Read note.txt in the current folder and reply with only the secret word it names."
JOBS = {
    "codex": {"backend": "codex", "effort": "low"},
    "neon": {"backend": "neon", "model": "flash"},
    "claude": {"backend": "claude", "model": "haiku", "effort": "low"},
}
WAIT_SECONDS = 240


async def main(names: list[str]) -> int:
    state = temp_dir("bridge-live-state-")
    work = temp_dir("bridge-live-cwd-")
    (work / "note.txt").write_text(f"The secret word is {WORD}.\n", encoding="utf-8")
    failed = 0
    async with stdio_client(server_params(state)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("tools:", sorted(tool.name for tool in (await session.list_tools()).tools))

            async def one(name: str) -> bool:
                args = {"prompt": PROMPT, "cwd": str(work), "mode": "read_only", "timeout_seconds": WAIT_SECONDS,
                        "wait_seconds": WAIT_SECONDS, **JOBS[name]}
                job = reply_data(await session.call_tool("run", args))
                # A wait the host cap cut short returns a receipt: collect the job until it ends.
                while job.get("status") in {"queued", "running"} and job.get("job_id"):
                    reply = reply_data(await session.call_tool("jobs", {"job_ids": [job["job_id"]], "wait_seconds": 60}))
                    job = (reply.get("jobs") or [job])[0]
                ok = job.get("status") == "completed" and WORD in (job.get("response") or "").lower()
                print("PASS" if ok else "FAIL", name, brief(job))
                return ok

            results = await asyncio.gather(*(one(name) for name in names), return_exceptions=True)
            for name, result in zip(names, results):
                if isinstance(result, BaseException):
                    print("FAIL", name, type(result).__name__, str(result)[:300])
                    failed += 1
                elif not result:
                    failed += 1
    print("state folder:", state)
    return 1 if failed else 0


if __name__ == "__main__":
    chosen = sys.argv[1:] or list(JOBS)
    unknown = [name for name in chosen if name not in JOBS]
    if unknown:
        sys.exit(f"unknown backend {unknown}; choose from {list(JOBS)}")
    sys.exit(asyncio.run(main(chosen)))
