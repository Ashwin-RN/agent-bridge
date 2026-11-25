"""Opt-in live subscription quota lookups for Codex (`app-server`) and Antigravity (`agy /usage`)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from agent_bridge.process_control import ManagedProcess, subprocess_creation_kwargs

logger = logging.getLogger("agent_bridge.quota")


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _epoch_to_iso(ts: int | float | None) -> str | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def _window_label(mins: int | float | None) -> str:
    if not isinstance(mins, (int, float)):
        return "unknown"
    m = int(mins)
    if m == 300:
        return "5h"
    if m == 10080:
        return "weekly"
    if m % 1440 == 0:
        return f"{m // 1440}d"
    if m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}m"


def parse_codex_rate_limits(result: dict[str, Any], *, fetched_at: str | None = None) -> dict[str, Any]:
    """Parse `codex app-server` `account/rateLimits/read` result into a clean quota summary."""
    ts = fetched_at or _iso_now()
    rl = result.get("rateLimits")
    if not isinstance(rl, dict):
        return {
            "backend": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Missing rateLimits object in Codex app-server response.",
        }

    windows: list[dict[str, Any]] = []
    for slot in ("primary", "secondary"):
        bucket = rl.get(slot)
        if not isinstance(bucket, dict):
            continue
        used = bucket.get("usedPercent")
        used_pct = round(float(used), 1) if isinstance(used, (int, float)) else None
        rem_pct = round(max(0.0, 100.0 - used_pct), 1) if used_pct is not None else None
        windows.append(
            {
                "slot": slot,
                "window": _window_label(bucket.get("windowDurationMins")),
                "used_percent": used_pct,
                "remaining_percent": rem_pct,
                "reset_time": _epoch_to_iso(bucket.get("resetsAt")),
            }
        )

    out: dict[str, Any] = {
        "backend": "codex",
        "status": "ok" if windows else "unknown",
        "fetched_at": ts,
        "plan_type": rl.get("planType"),
        "ordinary_usage_allowed": result.get("ordinaryUsageAllowed"),
        "rate_limit_reached": bool(rl.get("rateLimitReachedType")) or bool(rl.get("spendControlReached")),
        "windows": windows,
    }
    if rl.get("rateLimitReachedType"):
        out["rate_limit_reached_type"] = rl.get("rateLimitReachedType")
    return out


def parse_neon_usage_envelope(
    payload: dict[str, Any],
    *,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Parse `agy --output-format json --print=/usage` envelope (structured JSON + text fallback)."""
    ts = fetched_at or _iso_now()
    groups_out: list[dict[str, Any]] = []

    cmd_data = payload.get("command", {}).get("data") if isinstance(payload.get("command"), dict) else None
    raw_groups = cmd_data.get("groups") if isinstance(cmd_data, dict) else None

    if isinstance(raw_groups, list):
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            windows: list[dict[str, Any]] = []
            for b in g.get("buckets") or []:
                if not isinstance(b, dict):
                    continue
                rem_frac = b.get("remaining_fraction")
                rem_pct = round(float(rem_frac) * 100.0, 1) if isinstance(rem_frac, (int, float)) else None
                used_pct = round(max(0.0, 100.0 - rem_pct), 1) if rem_pct is not None else None
                reset = b.get("reset_time")
                if reset == "1970-01-01T00:00:00Z":
                    reset = None
                windows.append(
                    {
                        "id": b.get("id"),
                        "window": b.get("window") or b.get("name"),
                        "used_percent": used_pct,
                        "remaining_percent": rem_pct,
                        "reset_time": reset,
                    }
                )
            groups_out.append({"name": g.get("name", "unknown"), "windows": windows})

    # Fallback: parse tab-separated response text if command.data is absent.
    if not groups_out and isinstance(payload.get("response"), str):
        by_group: dict[str, list[dict[str, Any]]] = {}
        for line in payload["response"].splitlines():
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if len(parts) < 3:
                continue
            gname, wname, pct_raw = parts[0], parts[1], parts[2]
            reset = parts[3] if len(parts) >= 4 else None
            if reset == "1970-01-01T00:00:00Z":
                reset = None
            m = re.search(r"(\d+(?:\.\d+)?)%", pct_raw)
            rem_pct = round(float(m.group(1)), 1) if m else None
            used_pct = round(max(0.0, 100.0 - rem_pct), 1) if rem_pct is not None else None
            by_group.setdefault(gname, []).append(
                {
                    "window": wname,
                    "used_percent": used_pct,
                    "remaining_percent": rem_pct,
                    "reset_time": reset,
                }
            )
        groups_out = [{"name": k, "windows": v} for k, v in by_group.items()]

    if not groups_out:
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Could not parse quota buckets from agy /usage output.",
        }

    return {
        "backend": "neon",
        "status": "ok",
        "fetched_at": ts,
        "groups": groups_out,
    }


async def fetch_codex_quota(codex_cmd: list[str], *, timeout_seconds: float = 12.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `codex app-server` `account/rateLimits/read` RPC."""
    ts = _iso_now()
    managed: ManagedProcess | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *codex_cmd,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)
        assert proc.stdin is not None and proc.stdout is not None

        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-bridge", "version": "0.2.0"},
                    "capabilities": {},
                },
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ]
        payload = "".join(json.dumps(m) + "\n" for m in messages).encode("utf-8")
        proc.stdin.write(payload)
        await proc.stdin.drain()

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    "backend": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": f"Timed out reading Codex rate limits after {timeout_seconds:.0f}s.",
                }
            raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw_line:
                break
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text.startswith("{"):
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == 2:
                if "result" in msg and isinstance(msg["result"], dict):
                    return parse_codex_rate_limits(msg["result"], fetched_at=ts)
                err = msg.get("error", {})
                return {
                    "backend": "codex",
                    "status": "unknown",
                    "fetched_at": ts,
                    "error": str(err.get("message") if isinstance(err, dict) else err),
                }

        return {
            "backend": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": "Codex app-server closed before returning rate limits.",
        }
    except Exception as exc:
        return {
            "backend": "codex",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if managed is not None:
            await managed.terminate()


async def fetch_neon_quota(agy_cmd: list[str], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Perform a short-lived one-shot `agy --output-format json --print=/usage` probe."""
    ts = _iso_now()
    managed: ManagedProcess | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *agy_cmd,
            "--mode",
            "plan",
            "--output-format",
            "json",
            f"--print-timeout",
            f"{int(timeout_seconds)}s",
            "--print=/usage",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_creation_kwargs(),
        )
        managed = ManagedProcess(proc)
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds + 5.0)
        managed.close()

        stdout_text = stdout_b.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not stdout_text:
            err = stderr_b.decode("utf-8", errors="replace").strip() or f"agy /usage exited {proc.returncode}"
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": err[:400],
            }

        envelope: dict[str, Any] | None = None
        try:
            parsed = json.loads(stdout_text)
            if isinstance(parsed, dict):
                envelope = parsed
        except json.JSONDecodeError:
            for line in reversed(stdout_text.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        if isinstance(parsed, dict):
                            envelope = parsed
                            break
                    except json.JSONDecodeError:
                        continue

        if envelope is None:
            return {
                "backend": "neon",
                "status": "unknown",
                "fetched_at": ts,
                "error": "Non-JSON response from agy /usage.",
            }

        return parse_neon_usage_envelope(envelope, fetched_at=ts)
    except Exception as exc:
        if managed is not None:
            await managed.terminate()
        return {
            "backend": "neon",
            "status": "unknown",
            "fetched_at": ts,
            "error": f"{type(exc).__name__}: {exc}",
        }
