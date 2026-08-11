from __future__ import annotations

import os
import socket
from datetime import timedelta

import pytest

from smog_ai.database.engine import session_scope
from smog_ai.database.models import ProcessLock
from smog_ai.errors import LockUnavailable
from smog_ai.locking import (
    WAIT_ABANDONED,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ProcessLease,
    _WindowsMutex,
)
from smog_ai.time_utils import utc_now


class FakeKernel32:
    def __init__(self, wait_result: int) -> None:
        self.wait_result = wait_result
        self.released: list[int] = []
        self.closed: list[int] = []

    def CreateMutexW(self, _attributes, _initial_owner, _name):  # noqa: N802
        return 1234

    def WaitForSingleObject(self, _handle, _timeout):  # noqa: N802
        return self.wait_result

    def ReleaseMutex(self, handle):  # noqa: N802
        self.released.append(int(handle))
        return 1

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(int(handle))
        return 1


@pytest.mark.parametrize("wait_result", [WAIT_OBJECT_0, WAIT_ABANDONED])
def test_windows_mutex_acquires_signaled_or_abandoned_mutex(
    wait_result: int,
) -> None:
    api = FakeKernel32(wait_result)
    mutex = _WindowsMutex(
        "Global\\SmogAI-test",
        platform_name="nt",
        kernel32=api,
    )

    mutex.acquire()

    assert mutex.owned is True
    assert mutex.abandoned is (wait_result == WAIT_ABANDONED)

    mutex.release()

    assert api.released == [1234]
    assert api.closed == [1234]


def test_windows_mutex_times_out_only_when_actually_owned() -> None:
    api = FakeKernel32(WAIT_TIMEOUT)
    mutex = _WindowsMutex(
        "Global\\SmogAI-test",
        platform_name="nt",
        kernel32=api,
    )

    with pytest.raises(LockUnavailable):
        mutex.acquire()

    assert api.released == []
    assert api.closed == [1234]


def test_process_lease_takes_over_unexpired_dead_local_owner(
    engine,
    app_config,
) -> None:
    now = utc_now()

    with session_scope(engine) as session:
        session.add(
            ProcessLock(
                lock_name="dead-owner-lock",
                process_id=2_147_483_647,
                host_name=socket.gethostname(),
                owner_token="dead-owner",
                started_at=now - timedelta(minutes=10),
                heartbeat_at=now - timedelta(minutes=9),
                expires_at=now + timedelta(hours=1),
            )
        )

    lease = ProcessLease(
        engine,
        app_config,
        "dead-owner-lock",
    ).acquire()

    try:
        with session_scope(engine) as session:
            row = session.get(ProcessLock, "dead-owner-lock")
            assert row is not None
            assert row.process_id == os.getpid()
            assert row.owner_token == lease.owner_token
    finally:
        lease.release()

    with session_scope(engine) as session:
        assert session.get(ProcessLock, "dead-owner-lock") is None
