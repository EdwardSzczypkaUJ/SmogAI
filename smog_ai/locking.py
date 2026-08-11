from __future__ import annotations

import ctypes
import os
import socket
import threading
import uuid
from contextlib import AbstractContextManager
from datetime import timedelta
from types import TracebackType
from typing import Any

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from smog_ai.config import AppConfig
from smog_ai.database.models import ProcessLock
from smog_ai.database.repository import as_utc
from smog_ai.errors import LockUnavailable
from smog_ai.time_utils import utc_now

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


def _last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _load_kernel32() -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise RuntimeError("Win32 synchronization API is unavailable on this platform.")

    kernel32 = loader("kernel32", use_last_error=True)

    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_wchar_p,
    ]
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    kernel32.WaitForSingleObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int

    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p

    return kernel32


def _process_is_running(process_id: int) -> bool:
    """Conservative local-process liveness probe.

    A permissions error is treated as "running" so that the lock is never
    stolen from a process owned by another account.  A PID that cannot exist
    is treated as dead, which allows immediate recovery after a crash instead
    of waiting for the SQLite lease to expire.
    """

    if process_id <= 0:
        return False
    if process_id == os.getpid():
        return True

    if os.name == "nt":
        kernel32 = _load_kernel32()
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(process_id),
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True

        error = _last_error()
        if error == ERROR_INVALID_PARAMETER:
            return False
        if error == ERROR_ACCESS_DENIED:
            return True

        # Unknown Win32 errors are handled conservatively.
        return True

    try:
        os.kill(int(process_id), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


class _WindowsMutex:
    """A real owned Windows mutex, not merely a named-object existence check.

    CreateMutexW returns a handle to an existing mutex and sets
    ERROR_ALREADY_EXISTS even when that mutex is currently unowned.  Therefore
    existence is not equivalent to ownership.  The caller must wait on the
    handle to acquire it.

    WAIT_ABANDONED means that the previous owner terminated without releasing
    the mutex; Windows grants ownership to the current caller.  We accept that
    ownership and let the SQLite lease layer verify/recover persistent state.
    """

    def __init__(
        self,
        name: str,
        *,
        platform_name: str | None = None,
        kernel32: Any | None = None,
    ) -> None:
        self.name = name
        self.handle: Any = None
        self.owned = False
        self.abandoned = False
        self._platform_name = platform_name or os.name
        self._kernel32_override = kernel32

    def _api(self) -> Any:
        return self._kernel32_override or _load_kernel32()

    def acquire(self) -> None:
        if self._platform_name != "nt":
            return

        kernel32 = self._api()
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(_last_error(), "CreateMutexW failed")

        wait_result = int(kernel32.WaitForSingleObject(handle, 0))

        if wait_result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self.handle = handle
            self.owned = True
            self.abandoned = wait_result == WAIT_ABANDONED
            return

        kernel32.CloseHandle(handle)

        if wait_result == WAIT_TIMEOUT:
            raise LockUnavailable(f"Windows mutex is already held: {self.name}")

        if wait_result == WAIT_FAILED:
            raise OSError(_last_error(), "WaitForSingleObject failed")

        raise OSError(
            wait_result,
            f"Unexpected WaitForSingleObject result for {self.name}",
        )

    def release(self) -> None:
        if self._platform_name != "nt" or not self.handle:
            return

        kernel32 = self._api()
        handle = self.handle
        owned = self.owned

        self.handle = None
        self.owned = False
        self.abandoned = False

        try:
            if owned:
                kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)


class ProcessLease(AbstractContextManager["ProcessLease"]):
    """Double lock: an owned Windows mutex plus a renewable SQLite lease."""

    def __init__(
        self,
        engine: Engine,
        config: AppConfig,
        lock_name: str,
        *,
        heartbeat_enabled: bool = True,
    ) -> None:
        self.engine = engine
        self.config = config
        self.lock_name = lock_name
        self.heartbeat_enabled = bool(heartbeat_enabled)
        safe = "".join(char if char.isalnum() else "-" for char in lock_name)
        self.mutex = _WindowsMutex(
            f"{config.locking.windows_mutex_prefix}-{safe}"
        )
        self.owner_token = uuid.uuid4().hex
        self.process_id = os.getpid()
        self.host_name = socket.gethostname()
        self._factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            class_=Session,
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _same_host(left: str, right: str) -> bool:
        return left.strip().casefold() == right.strip().casefold()

    def acquire(self) -> "ProcessLease":
        self.mutex.acquire()
        now = utc_now()
        expires = now + timedelta(seconds=self.config.locking.lease_seconds)

        try:
            with self._factory.begin() as session:
                current = session.get(ProcessLock, self.lock_name)

                if (
                    current is not None
                    and as_utc(current.expires_at) > now
                    and current.owner_token != self.owner_token
                ):
                    local_owner = self._same_host(
                        current.host_name,
                        self.host_name,
                    )
                    owner_is_alive = (
                        _process_is_running(current.process_id)
                        if local_owner
                        else True
                    )

                    if owner_is_alive:
                        raise LockUnavailable(
                            f"Lock {self.lock_name!r} held by PID "
                            f"{current.process_id} on {current.host_name} "
                            f"until {current.expires_at.isoformat()}"
                        )

                    # The owner was on this host but its PID no longer exists.
                    # Recover immediately rather than waiting for lease expiry.

                if current is None:
                    current = ProcessLock(
                        lock_name=self.lock_name,
                        process_id=self.process_id,
                        host_name=self.host_name,
                        owner_token=self.owner_token,
                        started_at=now,
                        heartbeat_at=now,
                        expires_at=expires,
                    )
                    session.add(current)
                else:
                    current.process_id = self.process_id
                    current.host_name = self.host_name
                    current.owner_token = self.owner_token
                    current.started_at = now
                    current.heartbeat_at = now
                    current.expires_at = expires
        except Exception:
            self.mutex.release()
            raise

        if self.heartbeat_enabled:
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"lease-{self.lock_name}",
                daemon=True,
            )
            self._thread.start()
        return self

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.config.locking.heartbeat_seconds):
            now = utc_now()
            try:
                with self._factory.begin() as session:
                    row = session.get(ProcessLock, self.lock_name)
                    if row is None or row.owner_token != self.owner_token:
                        return
                    row.heartbeat_at = now
                    row.expires_at = now + timedelta(
                        seconds=self.config.locking.lease_seconds
                    )
            except Exception:
                # Expiry still guarantees eventual recovery if a heartbeat
                # cannot be written.
                continue

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(
                timeout=max(
                    1.0,
                    self.config.locking.heartbeat_seconds / 2,
                )
            )

        try:
            with self._factory.begin() as session:
                session.execute(
                    delete(ProcessLock).where(
                        ProcessLock.lock_name == self.lock_name,
                        ProcessLock.owner_token == self.owner_token,
                    )
                )
        finally:
            self.mutex.release()

    def __enter__(self) -> "ProcessLease":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
