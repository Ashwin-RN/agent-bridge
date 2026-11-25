"""Cross-platform process lifecycle, OS liveness checks, and single-owner tree termination."""

from __future__ import annotations

import asyncio
import contextvars
import ctypes
import logging
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("agent_bridge.process_control")

# Windows Win32 constants
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


if sys.platform == "win32":
    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL


def is_pid_alive(pid: int) -> bool:
    """Return True if an OS process with `pid` is currently alive."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = ctypes.get_last_error()
            return err == _ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD(0)
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def is_pgid_alive(pgid: int) -> bool:
    """POSIX only: return True if ANY process in process group `pgid` is still alive."""
    if pgid <= 0 or sys.platform == "win32":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def subprocess_creation_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class ProcessTracker:
    """Tracks every subprocess spawned for one job so cleanup and stop verification are airtight."""

    def __init__(self) -> None:
        self._active: set[ManagedProcess] = set()
        self._unverified_stop: bool = False

    def register(self, managed: ManagedProcess) -> None:
        self._active.add(managed)

    def unregister(self, managed: ManagedProcess, *, verified_stopped: bool = True) -> None:
        self._active.discard(managed)
        if not verified_stopped:
            self._unverified_stop = True

    def has_alive_processes(self) -> bool:
        return any(m.is_alive() for m in list(self._active))

    def terminate_all_sync(self) -> None:
        for managed in list(self._active):
            managed.terminate_sync()

    async def terminate_all_and_verify(self) -> bool:
        """Terminate all remaining tracked processes and return True only if all are verified stopped."""
        all_stopped = True
        for managed in list(self._active):
            try:
                stopped = await managed.terminate()
            except BaseException:
                managed.terminate_sync()
                stopped = not managed.is_alive()
            if not stopped:
                all_stopped = False
        return all_stopped and not self._unverified_stop and not self.has_alive_processes()


CURRENT_PROCESS_TRACKER: contextvars.ContextVar[ProcessTracker | None] = contextvars.ContextVar(
    "agent_bridge_process_tracker",
    default=None,
)


class ManagedProcess:
    """Single-owner subprocess wrapper that guarantees descendant cleanup and verifies exit."""

    _POSIX_GRACE_SECONDS: float = 2.5
    _POSIX_KILL_WAIT_SECONDS: float = 3.0

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        tracker: ProcessTracker | None = None,
    ) -> None:
        self.proc = proc
        self._job_handle: int | None = None
        self._pgid: int | None = None
        self._closed_cleanly = False
        self._terminated_verified = False
        self._tracker = tracker if tracker is not None else CURRENT_PROCESS_TRACKER.get()

        if sys.platform == "win32":
            self._attach_windows_job_object(proc.pid)
        else:
            # With start_new_session=True, pgid equals child's pid.
            self._pgid = proc.pid

        if self._tracker is not None:
            self._tracker.register(self)

    def _attach_windows_job_object(self, pid: int) -> None:
        try:
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                _kernel32.CloseHandle(job)
                return

            proc_handle = _kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid)
            )
            if not proc_handle:
                _kernel32.CloseHandle(job)
                return
            try:
                if not _kernel32.AssignProcessToJobObject(job, proc_handle):
                    _kernel32.CloseHandle(job)
                    return
            finally:
                _kernel32.CloseHandle(proc_handle)

            self._job_handle = int(job)
        except Exception as exc:
            logger.debug("Could not attach Windows Job Object to pid %s: %s", pid, exc)

    def is_alive(self) -> bool:
        """Return True if the child process OR (on POSIX) any descendant in its process group is alive."""
        if sys.platform != "win32":
            pgid = self._pgid or self.proc.pid
            if pgid and is_pgid_alive(pgid):
                return True
        if self.proc.returncode is not None:
            return False
        if self.proc.pid is None:
            return False
        return is_pid_alive(self.proc.pid)

    def terminate_sync(self) -> None:
        """Best-effort synchronous kill safe to call even during nested cancellation."""
        if sys.platform == "win32":
            if self._job_handle is not None:
                try:
                    _kernel32.TerminateJobObject(wintypes.HANDLE(self._job_handle), 1)
                except Exception:
                    pass
                finally:
                    try:
                        _kernel32.CloseHandle(wintypes.HANDLE(self._job_handle))
                    except Exception:
                        pass
                    self._job_handle = None
            if self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
        else:
            pgid = self._pgid or self.proc.pid
            sigkill = getattr(signal, "SIGKILL", 9)
            if pgid:
                try:
                    os.killpg(pgid, sigkill)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            if self.proc.returncode is None:
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass

    async def terminate(self) -> bool:
        """Terminate the process and all descendants cleanly, returning True only if verified stopped."""
        if self._terminated_verified or (self._closed_cleanly and not self.is_alive()):
            if self._tracker is not None:
                self._tracker.unregister(self, verified_stopped=True)
            return True

        try:
            if sys.platform == "win32":
                if self._job_handle is not None:
                    try:
                        _kernel32.TerminateJobObject(wintypes.HANDLE(self._job_handle), 1)
                    except Exception:
                        pass
                    finally:
                        try:
                            _kernel32.CloseHandle(wintypes.HANDLE(self._job_handle))
                        except Exception:
                            pass
                        self._job_handle = None

                if self.proc.returncode is None:
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        pass

                if self.proc.returncode is None and self.proc.pid:
                    try:
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill",
                            "/PID",
                            str(self.proc.pid),
                            "/T",
                            "/F",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(killer.wait(), timeout=5.0)
                    except Exception:
                        try:
                            self.proc.kill()
                        except ProcessLookupError:
                            pass
                        except Exception:
                            pass
            else:
                pgid = self._pgid or self.proc.pid
                if pgid:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        if self.proc.returncode is None:
                            try:
                                self.proc.terminate()
                            except ProcessLookupError:
                                pass

                # Wait through the grace period until BOTH leader AND entire process group have exited.
                grace_deadline = time.monotonic() + self._POSIX_GRACE_SECONDS
                if self.proc.returncode is None:
                    rem = max(0.0, grace_deadline - time.monotonic())
                    try:
                        await asyncio.wait_for(self.proc.wait(), timeout=rem)
                    except asyncio.TimeoutError:
                        pass
                while pgid and is_pgid_alive(pgid) and time.monotonic() < grace_deadline:
                    await asyncio.sleep(min(0.05, max(0.0, grace_deadline - time.monotonic())))

                # Escalate to SIGKILL if the leader OR any descendant in the process group survived SIGTERM!
                if self.proc.returncode is None or (pgid and is_pgid_alive(pgid)):
                    sigkill = getattr(signal, "SIGKILL", 9)
                    if pgid:
                        try:
                            os.killpg(pgid, sigkill)
                        except ProcessLookupError:
                            pass
                        except Exception:
                            if self.proc.returncode is None:
                                try:
                                    self.proc.kill()
                                except ProcessLookupError:
                                    pass
                    elif self.proc.returncode is None:
                        try:
                            self.proc.kill()
                        except ProcessLookupError:
                            pass

                    kill_deadline = time.monotonic() + self._POSIX_KILL_WAIT_SECONDS
                    if self.proc.returncode is None:
                        rem = max(0.0, kill_deadline - time.monotonic())
                        try:
                            await asyncio.wait_for(self.proc.wait(), timeout=rem)
                        except asyncio.TimeoutError:
                            pass
                    while pgid and is_pgid_alive(pgid) and time.monotonic() < kill_deadline:
                        await asyncio.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))

            if self.proc.returncode is None:
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
        except BaseException:
            # Even if terminate() itself is cancelled mid-await, synchronously force-kill the tree.
            self.terminate_sync()
            stopped_now = not self.is_alive()
            self._terminated_verified = stopped_now
            if self._tracker is not None:
                self._tracker.unregister(self, verified_stopped=stopped_now)
            raise

        stopped = not self.is_alive()
        self._terminated_verified = stopped
        if self._tracker is not None:
            self._tracker.unregister(self, verified_stopped=stopped)
        return stopped

    def close(self) -> None:
        """Release kernel handles after a normal exit (also reaps lingering descendants on Windows)."""
        if self._closed_cleanly:
            return
        self._closed_cleanly = True
        if sys.platform == "win32" and self._job_handle is not None:
            try:
                _kernel32.CloseHandle(wintypes.HANDLE(self._job_handle))
            except Exception:
                pass
            self._job_handle = None
        if self._tracker is not None:
            self._tracker.unregister(self, verified_stopped=not self.is_alive())
