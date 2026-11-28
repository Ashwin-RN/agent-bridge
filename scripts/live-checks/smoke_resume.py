"""Two turns on each backend through the real CLIs: the second resumes the first and must recall a word.

Run from the checkout:  uv run python scripts/live-checks/smoke_resume.py [backend ...]
Passes when the second turn is `completed`, keeps the first turn's session id, and replies with the word.
Spends a little quota on each subscription it uses. Exit code 1 if any backend did not pass.
"""

from __future__ import annotations

import asyncio
import sys

from _bridge import brief, reply_data, server_params, temp_dir
from mcp import ClientSession
from mcp.client.stdio import stdio_client

WORD = "saffron"
JOBS = {
    "codex": {"backend": "codex", "effort": "low"},
    "neon": {"backend": "neon", "model": "flash"},
    "claude": {"backend": "claude", "model": "haiku", "effort": "low"},
}
WAIT = 240


async def main(names: list[str]) -> int:
    state = temp_dir("bridge-live-state-")
    work = temp_dir("bridge-live-cwd-")
    failed = 0
    async with stdio_client(server_params(state)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def finish(job: dict) -> dict:
                while job.get("status") in {"queued", "running"} and job.get("job_id"):
                    reply = reply_data(await session.call_tool("jobs", {"job_ids": [job["job_id"]], "wait_seconds": 60}))
                    job = (reply.get("jobs") or [job])[0]
                return job

            async def one(name: str) -> bool:
                first = await finish(reply_data(await session.call_tool("run", {
                    "prompt": f"Remember this word for later: {WORD}. Use no tools and read no files. Reply with only OK.", "cwd": str(work),
                    "timeout_seconds": WAIT, "wait_seconds": WAIT, **JOBS[name]})))
                if first.get("status") != "completed" or not first.get("session_id"):
                    print("FAIL", name, "first turn", brief(first), "session_id:", first.get("session_id"))
                    return False
                second = await finish(reply_data(await session.call_tool("run", {
                    "resume_from": first["job_id"], "prompt": "Which word did I ask you to remember? Reply with only that word.",
                    "timeout_seconds": WAIT, "wait_seconds": WAIT})))
                ok = (second.get("status") == "completed" and WORD in (second.get("response") or "").lower()
                      and second.get("session_id") == first.get("session_id"))
                print("PASS" if ok else "FAIL", name, "resume", brief(second), "same session:", second.get("session_id") == first.get("session_id"))
                return ok

            results = await asyncio.gather(*(one(name) for name in names), return_exceptions=True)
            for name, result in zip(names, results):
                if isinstance(result, BaseException):
                    print("FAIL", name, type(result).__name__, str(result)[:300])
                    failed += 1
                elif not result:
                    failed += 1
            usage = reply_data(await session.call_tool("usage", {"period": "all"}))
            print("usage by pool:", {k: (v.get("runs"), v.get("total_tokens")) for k, v in (usage.get("by_quota_pool") or {}).items()})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:] or list(JOBS))))
