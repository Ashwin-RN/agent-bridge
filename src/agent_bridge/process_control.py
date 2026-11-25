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
from collections.abc import Callable, Coroutine
from ctypes import wintypes
from typing import Any

logger = logging.getLogger("agent_bridge.process_control")

_LAUNCH_CANCEL_WAIT_SECONDS = 3.0


class ProcessContainmentError(RuntimeError):
    """Raised when a subprocess cannot be contained (or resumed after containment) before execution."""

    def __init__(self, message: str, *, process_still_alive: bool = False) -> None:
        self.process_still_alive = process_still_alive
        super().__init__(message)

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
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
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

    def mark_unverified_stop(self) -> None:
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
        require_containment: bool | None = None,
    ) -> None:
        self.proc = proc
        self._job_handle: int | None = None
        self._pgid: int | None = None
        self._closed_cleanly = False
        self._terminated_verified = False
        self._tracker = tracker if tracker is not None else CURRENT_PROCESS_TRACKER.get()
        enforce_containment = resume_suspended if require_containment is None else require_containment

        if sys.platform == "win32":
            self._attach_windows_job_object(
                proc.pid,
                resume_suspended=resume_suspended,
                require_containment=enforce_containment,
            )
        else:
            # With start_new_session=True, pgid equals child's pid from instruction 0.
            self._pgid = proc.pid

        if self._tracker is not None:
            self._tracker.register(self)

    @property
    def verified_stopped(self) -> bool:
        return self._terminated_verified or (self._closed_cleanly and not self.is_alive())

    def _attach_windows_job_object(
        self,
        pid: int,
        *,
        resume_suspended: bool = False,
        require_containment: bool = False,
    ) -> None:
        proc_handle = None
        job = None
        failure_reason: str | None = None
        try:
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
            if resume_suspended:
                access |= _PROCESS_SUSPEND_RESUME
            proc_handle = _kernel32.OpenProcess(access, False, int(pid))
            if not proc_handle:
                err = ctypes.get_last_error()
                failure_reason = f"OpenProcess failed (winerr={err})"
            else:
                job = _kernel32.CreateJobObjectW(None, None)
                if not job:
                    err = ctypes.get_last_error()
                    failure_reason = f"CreateJobObjectW failed (winerr={err})"
                else:
                    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    if not _kernel32.SetInformationJobObject(
                        job,
                        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                        ctypes.byref(info),
                        ctypes.sizeof(info),
                    ):
                        err = ctypes.get_last_error()
                        failure_reason = f"SetInformationJobObject failed (winerr={err})"
                    elif not _kernel32.AssignProcessToJobObject(job, proc_handle):
                        err = ctypes.get_last_error()
                        failure_reason = f"AssignProcessToJobObject failed (winerr={err})"
                    else:
                        self._job_handle = int(job)
                        job = None  # Ownership transferred to self._job_handle
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            logger.debug("Could not attach Windows Job Object to pid %s: %s", pid, exc)
        finally:
            if job:
                try:
                    _kernel32.CloseHandle(job)
                except Exception:
                    pass

        # Fail closed: if containment failed while required, NEVER resume the still-suspended worker!
        if self._job_handle is None and require_containment:
            try:
                if proc_handle:
                    try:
                        _kernel32.TerminateProcess(wintypes.HANDLE(proc_handle), 1)
                    except Exception:
                        pass
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    pass
            finally:
                if proc_handle:
                    try:
                        _kernel32.CloseHandle(proc_handle)
                    except Exception:
                        pass
            raise ProcessContainmentError(
                f"Failed to contain Windows subprocess pid={pid} in a Job Object "
                f"({failure_reason or 'unknown error'}); terminated suspended worker before execution."
            )

        try:
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
                            _kernel32.CloseHandle(wintypes.HANDLE(self._job_handle))
                        except Exception:
                            pass
                        self._job_handle = None
                    if proc_handle:
                        try:
                            _kernel32.TerminateProcess(wintypes.HANDLE(proc_handle), 1)
                        except Exception:
                            pass
                    try:
                        self.proc.kill()
                    except ProcessLookupError:
                        pass
                    except Exception:
                        pass
                    if require_containment:
                        raise ProcessContainmentError(
                            f"Failed to resume suspended Windows subprocess pid={pid} after Job Object "
                            "attachment; terminated worker before execution."
                        )
        finally:
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
    """Spawn and register a contained subprocess as one cancellation-safe operation.

    - Windows: spawns `CREATE_SUSPENDED`, attaches a Job Object before executing any instruction,
      and fails closed (terminating the still-suspended worker) if containment fails.
    - POSIX: runs `create_subprocess_exec` + `ManagedProcess` registration inside an uncancelled
      task so cancellation during asyncio pipe setup still captures and terminates the process group.
    """
    use_suspended = sys.platform == "win32"
    resolved_tracker = tracker if tracker is not None else CURRENT_PROCESS_TRACKER.get()
    caller_cancelled = False

    async def _reap_failed_windows_proc(proc: asyncio.subprocess.Process) -> bool:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
        if proc.returncode is None and proc.pid and is_pid_alive(proc.pid):
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5.0)
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                pass
        still_alive = bool(proc.pid and is_pid_alive(proc.pid))
        if still_alive and resolved_tracker is not None:
            resolved_tracker.mark_unverified_stop()
        return still_alive

    async def _launch() -> tuple[asyncio.subprocess.Process, ManagedProcess] | None:
        if caller_cancelled:
            return None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            **subprocess_creation_kwargs(suspended=use_suspended),
        )
        # If caller cancelled while Windows process was still suspended, kill it without resuming.
        if caller_cancelled and use_suspended:
            await _reap_failed_windows_proc(proc)
            return None
        try:
            managed = ManagedProcess(
                proc,
                tracker=resolved_tracker,
                resume_suspended=use_suspended,
                require_containment=use_suspended,
            )
        except ProcessContainmentError as exc:
            still_alive = await _reap_failed_windows_proc(proc)
            if still_alive != exc.process_still_alive:
                raise ProcessContainmentError(str(exc), process_still_alive=still_alive) from exc
            raise
        return proc, managed

    launch_task: asyncio.Task[tuple[asyncio.subprocess.Process, ManagedProcess] | None] = (
        asyncio.create_task(_launch())
    )
    try:
        result = await asyncio.shield(launch_task)
    except BaseException:
        caller_cancelled = True
        # Wait briefly (shielded) so POSIX pipe setup + process-group registration finish atomically.
        wait_deadline = time.monotonic() + _LAUNCH_CANCEL_WAIT_SECONDS
        while not launch_task.done():
            rem = wait_deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                await asyncio.wait({launch_task}, timeout=rem)
            except asyncio.CancelledError:
                continue

        if not launch_task.done():
            if resolved_tracker is not None:
                resolved_tracker.mark_unverified_stop()

            def _late_cleanup(t: asyncio.Task[Any]) -> None:
                if t.cancelled():
                    return
                try:
                    res = t.result()
                except BaseException:
                    return
                if res is not None:
                    res[1].terminate_sync()

            launch_task.add_done_callback(_late_cleanup)
            launch_task.cancel()
            try:
                await asyncio.wait({launch_task}, timeout=1.0)
            except BaseException:
                pass
            raise

        if not launch_task.cancelled():
            launch_exc = launch_task.exception()
            if launch_exc is None:
                launched = launch_task.result()
                if launched is not None:
                    _, managed = launched
                    try:
                        await managed.terminate()
                    except BaseException:
                        managed.terminate_sync()
                        if managed.is_alive() and resolved_tracker is not None:
                            resolved_tracker.mark_unverified_stop()
            elif not isinstance(
                launch_exc,
                (FileNotFoundError, PermissionError, NotADirectoryError, ProcessContainmentError),
            ):
                if resolved_tracker is not None:
                    resolved_tracker.mark_unverified_stop()
        raise

    if result is None:
        raise asyncio.CancelledError()
    return result


async def _wait_for_leader_exit(proc: asyncio.subprocess.Process) -> int:
    """Wait for the subprocess leader itself to exit, independent of descendant-held pipe EOF.

    Python's `asyncio.subprocess.Process.wait()` waits for `BaseSubprocessTransport._try_finish()`,
    which blocks until ALL stdout/stderr pipes hit EOF too. By observing `proc.returncode` (set
    immediately inside `BaseSubprocessTransport._process_exited`) and hooking `_protocol.process_exited`,
    we detect leader exit right away even when a child process inherited and kept pipes open.
    """
    if proc.returncode is not None:
        return proc.returncode

    exited_event = asyncio.Event()
    proto = getattr(proc, "_protocol", None)
    orig_cb = getattr(proto, "process_exited", None)
    hooked = proto is not None and callable(orig_cb)
    if hooked:
        def _on_exited() -> None:
            try:
                orig_cb()
            finally:
                exited_event.set()

        setattr(proto, "process_exited", _on_exited)

    try:
        while proc.returncode is None:
            try:
                await asyncio.wait_for(exited_event.wait(), timeout=0.05)
            except asyncio.TimeoutError:
                pass
        return proc.returncode
    finally:
        if hooked:
            setattr(proto, "process_exited", orig_cb)


async def run_managed_process_io(
    proc: asyncio.subprocess.Process,
    managed: ManagedProcess,
    *,
    read_stdout: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    read_stderr: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    write_stdin: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    timeout: float,
) -> bool:
    """Await leader exit independently of pipe EOF, reap descendants, then drain remaining output.

    Prevents a normally exiting worker from being misclassified as timed out when a descendant
    inherits its stdout/stderr pipe handles and keeps them open.
    Returns `True` only when leader and all descendants are verified stopped.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    wait_task: asyncio.Task[int] = asyncio.create_task(_wait_for_leader_exit(proc))
    stdin_task = asyncio.create_task(write_stdin()) if write_stdin is not None else None
    stdout_task = asyncio.create_task(read_stdout()) if read_stdout is not None else None
    stderr_task = asyncio.create_task(read_stderr()) if read_stderr is not None else None

    all_tasks: list[asyncio.Task[Any]] = [
        t for t in (wait_task, stdin_task, stdout_task, stderr_task) if t is not None
    ]
    io_tasks: list[asyncio.Task[Any]] = [
        t for t in (stdin_task, stdout_task, stderr_task) if t is not None
    ]
    read_tasks: list[asyncio.Task[Any]] = [
        t for t in (stdout_task, stderr_task) if t is not None
    ]

    try:
        # Phase 1: Monitor leader exit and IO errors within `timeout`.
        while not wait_task.done():
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise asyncio.TimeoutError()
            pending = {wait_task, *(t for t in io_tasks if not t.done())}
            done, _ = await asyncio.wait(
                pending,
                timeout=rem,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError()
            for task in done:
                if task is not wait_task and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        raise exc

        # Phase 2: Leader exited — stop stdin writer if still pending.
        if stdin_task is not None and not stdin_task.done():
            stdin_task.cancel()
            await asyncio.gather(stdin_task, return_exceptions=True)

        # Allow a tiny natural-EOF window before reaping lingering descendants.
        pending_reads = [t for t in read_tasks if not t.done()]
        if pending_reads:
            quick_grace = (
                0.05
                if managed.is_alive()
                else min(0.25, max(0.05, deadline - time.monotonic()))
            )
            await asyncio.wait(pending_reads, timeout=quick_grace)

        # Phase 3: Reap any lingering descendants so inherited pipe write ends close!
        process_stopped = await managed.finish()

        # Phase 4: Drain all remaining buffered stdout/stderr output now that descendants stopped.
        pending_reads = [t for t in read_tasks if not t.done()]
        if pending_reads:
            drain_timeout = 2.5 if process_stopped else 0.25
            _, still_pending = await asyncio.wait(pending_reads, timeout=drain_timeout)
            if still_pending:
                for t in still_pending:
                    t.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

        for t in read_tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    raise exc

        if process_stopped:
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except (asyncio.TimeoutError, Exception):
                pass

        return process_stopped
    except BaseException:
        for t in all_tasks:
            if not t.done():
                t.cancel()
        try:
            await managed.terminate()
        finally:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        raise


async def communicate_managed(
    proc: asyncio.subprocess.Process,
    managed: ManagedProcess,
    *,
    timeout: float,
) -> tuple[bytes, bytes, bool]:
    """Read stdout/stderr while detecting leader exit independently of descendant-held pipes."""
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []

    async def _read_out() -> None:
        if proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            out_chunks.append(chunk)

    async def _read_err() -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(8192)
            if not chunk:
                break
            err_chunks.append(chunk)

    stopped = await run_managed_process_io(
        proc,
        managed,
        read_stdout=_read_out if proc.stdout is not None else None,
        read_stderr=_read_err if proc.stderr is not None else None,
        timeout=timeout,
    )
    return b"".join(out_chunks), b"".join(err_chunks), stopped
