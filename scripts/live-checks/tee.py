"""Run a stdio server and copy every line in each direction to a log file. Throwaway probe helper."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

LOG = os.environ.get("PROBE_WIRE_LOG", "wire.log")
lock = threading.Lock()


def note(direction: str, line: bytes) -> None:
    with lock, open(LOG, "ab") as fh:
        fh.write(f"{time.time():.2f} {direction} ".encode() + line.rstrip(b"\r\n") + b"\n")


def pump(src, dst, direction: str) -> None:
    for line in iter(src.readline, b""):
        note(direction, line)
        dst.write(line)
        dst.flush()
    try:
        dst.close()
    except OSError:
        pass


child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
threading.Thread(target=pump, args=(sys.stdin.buffer, child.stdin, "C>S"), daemon=True).start()
pump(child.stdout, sys.stdout.buffer, "S>C")
sys.exit(child.wait())
