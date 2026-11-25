"""Cross-platform process containment, OS liveness checks, and verified tree termination."""

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
_PROCESS_SUSPEND_RESUME = 0x0800
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_CREATE_SUSPENDED = 0x00000004
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
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

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
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

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
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
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
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
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD

    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


def _resume_suspended_windows_process(pid: int, proc_handle: int | None = None) -> bool:
    """Resume a process spawned with `CREATE_SUSPENDED` via NtResumeProcess or Toolhelp thread scan."""
    if sys.platform != "win32":
        return True
    owned_handle = None
    handle = proc_handle
    try:
        if not handle:
            owned_handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, int(pid))
            handle = owned_handle
        if handle:
            status = _ntdll.NtResumeProcess(wintypes.HANDLE(handle))
            if status == 0:
                return True
    except Exception as exc:
        logger.debug("NtResumeProcess failed for pid %s: %s", pid, exc)
    finally:
        if owned_handle:
            _kernel32.CloseHandle(owned_handle)

    # Fallback: enumerate threads via CreateToolhelp32Snapshot and call ResumeThread on matching PID.
    resumed = False
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return False
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        ok = _kernel32.Thread32First(snap, ctypes.byref(entry))
        while ok:
            if int(entry.th32OwnerProcessID) == int(pid):
                th = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, int(entry.th32ThreadID))
                if th:
                    try:
                        if _kernel32.ResumeThread(th) != 0xFFFFFFFF:
                            resumed = True
                    finally:
                        _kernel32.CloseHandle(th)
            ok = _kernel32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return resumed


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


def subprocess_creation_kwargs(*, suspended: bool = False) -> dict[str, Any]:
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if suspended:
            flags |= _CREATE_SUSPENDED
        return {"creationflags": flags}
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
    """Single-owner subprocess wrapper that guarantees pre-execution containment and verified tree cleanup."""

    _POSIX_GRACE_SECONDS: float = 2.5
    _POSIX_KILL_WAIT_SECONDS: float = 3.0
    _WIN_JOB_WAIT_SECONDS: float = 2.0

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        *,
        tracker: ProcessTracker | None = None,
        resume_suspended: bool = False,
    ) -> None:
        self.proc = proc
        self._job_handle: int | None = None
        self._pgid: int | None = None
        self._closed_cleanly = False
        self._terminated_verified = False
        self._tracker = tracker if tracker is not None else CURRENT_PROCESS_TRACKER.get()

        if sys.platform == "win32":
            self._attach_windows_job_object(proc.pid, resume_suspended=resume_suspended)
        else:
            # With start_new_session=True, pgid equals child's pid from instruction 0.
            self._pgid = proc.pid

        if self._tracker is not None:
            self._tracker.register(self)

    def _attach_windows_job_object(self, pid: int, *, resume_suspended: bool = False) -> None:
        proc_handle = None
        job = None
        try:
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
            if resume_suspended:
                access |= _PROCESS_SUSPEND_RESUME
            proc_handle = _kernel32.OpenProcess(access, False, int(pid))

            job = _kernel32.CreateJobObjectW(None, None)
            if job and proc_handle:
                info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if _kernel32.SetInformationJobObject(
                    job,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                ) and _kernel32.AssignProcessToJobObject(job, proc_handle):
                    self._job_handle = int(job)
                    job = None  # Ownership transferred to self._job_handle
        except Exception as exc:
            logger.debug("Could not attach Windows Job Object to pid %s: %s", pid, exc)
        finally:
            if job:
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass
            if resume_suspended:
                resumed = _resume_suspended_windows_process(pid, proc_handle)
                if not resumed:
                    logger.warning("Failed to resume suspended process pid %s; terminating it.", pid)
                    if self._job_handle is not None:
                        try:
                            _kernel32.TerminateJobObject(wintypes.HANDLE(self._job_handle), 1)
                        except Exception:
                            pass
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            if proc_handle:
                try:
                    _kernel32.CloseHandle(proc_handle)
                except Exception:
                    pass

    def _windows_job_active_processes(self) -> int | None:
        if sys.platform != "win32" or self._job_handle is None:
            return None
        try:
            acct = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
            ret_len = wintypes.DWORD(0)
            ok = _kernel32.QueryInformationJobObject(
                wintypes.HANDLE(self._job_handle),
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(acct),
                ctypes.sizeof(acct),
                ctypes.byref(ret_len),
            )
            if ok:
                return int(acct.ActiveProcesses)
        except Exception:
            pass
        return None

    def is_alive(self) -> bool:
        """Return True if the child process OR any contained descendant is still alive."""
        if sys.platform == "win32":
            active = self._windows_job_active_processes()
            if active is not None and active > 0:
                return True
        else:
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

        win_job_drained = True
        try:
            if sys.platform == "win32":
                if self._job_handle is not None:
                    try:
                        _kernel32.TerminateJobObject(wintypes.HANDLE(self._job_handle), 1)
                    except Exception:
                        pass

                    # Wait while handle is still open so we can verify ActiveProcesses == 0!
                    drain_deadline = time.monotonic() + self._WIN_JOB_WAIT_SECONDS
                    if self.proc.returncode is None:
                        rem = max(0.0, drain_deadline - time.monotonic())
                        try:
                            await asyncio.wait_for(self.proc.wait(), timeout=rem)
                        except asyncio.TimeoutError:
                            pass
                    while time.monotonic() < drain_deadline:
                        active = self._windows_job_active_processes()
                        if active is None or active == 0:
                            break
                        await asyncio.sleep(min(0.05, max(0.0, drain_deadline - time.monotonic())))

                    active_final = self._windows_job_active_processes()
                    if active_final is not None and active_final > 0:
                        win_job_drained = False

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
            if self._tracker is not None and stopped_now:
                self._tracker.unregister(self, verified_stopped=True)
            raise

        stopped = win_job_drained and (not self.is_alive())
        self._terminated_verified = stopped
        if self._tracker is not None:
            if stopped:
                self._tracker.unregister(self, verified_stopped=True)
            else:
                # Keep tracking unstopped processes so tracker still knows descendants survive!
                self._tracker._unverified_stop = True
        return stopped

    async def finish(self) -> bool:
        """Verify no descendants outlived a normal exit; terminate and verify if any remain."""
        if not self.is_alive():
            self.close()
            return True
        # A descendant survived after the leader exited normally: reap and verify cleanup!
        return await self.terminate()

    def close(self) -> None:
        """Release kernel handles after verified exit; keep tracking if descendants remain alive."""
        if self._closed_cleanly:
            return
        alive_before_close = self.is_alive()
        if sys.platform == "win32" and self._job_handle is not None:
            try:
                _kernel32.CloseHandle(wintypes.HANDLE(self._job_handle))
            except Exception:
                pass
            self._job_handle = None
        if not alive_before_close:
            self._closed_cleanly = True
            if self._tracker is not None:
                self._tracker.unregister(self, verified_stopped=True)
        elif self._tracker is not None:
            self._tracker._unverified_stop = True


async def spawn_managed_process(
    *cmd: str,
    cwd: str | None = None,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    tracker: ProcessTracker | None = None,
) -> tuple[asyncio.subprocess.Process, ManagedProcess]:
    """Spawn a subprocess already contained in a Windows Job Object (via CREATE_SUSPENDED) or POSIX session."""
    use_suspended = sys.platform == "win32"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        **subprocess_creation_kwargs(suspended=use_suspended),
    )
    managed = ManagedProcess(proc, tracker=tracker, resume_suspended=use_suspended)
    return proc, managed
