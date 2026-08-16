from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from smog_ai import cli
from smog_ai.database.engine import session_scope
from smog_ai.database.models import ProcessLock
from smog_ai.domain import StageStats
from smog_ai.locking import ProcessLease


def test_process_lease_can_disable_database_heartbeat(engine, app_config) -> None:  # type: ignore[no-untyped-def]
    lease = ProcessLease(
        engine,
        app_config,
        "quiet-snapshot-lock",
        heartbeat_enabled=False,
    ).acquire()
    try:
        assert lease.heartbeat_enabled is False
        assert lease._thread is None
        with session_scope(engine) as session:
            row = session.get(ProcessLock, "quiet-snapshot-lock")
            assert row is not None
            assert row.owner_token == lease.owner_token
    finally:
        lease.release()

    with session_scope(engine) as session:
        assert session.get(ProcessLock, "quiet-snapshot-lock") is None


def test_standalone_snapshot_command_uses_quiet_lease(
    engine,
    app_config,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    lease_stack: list[SimpleNamespace] = []
    leases: list[SimpleNamespace] = []

    class FakeLease:
        def __init__(
            self,
            _engine,
            _config,
            lock_name: str,
            *,
            heartbeat_enabled: bool = True,
        ) -> None:
            self.record = SimpleNamespace(
                lock_name=lock_name,
                heartbeat_enabled=heartbeat_enabled,
            )
            leases.append(self.record)

        def __enter__(self):
            lease_stack.append(self.record)
            return self

        def __exit__(self, *_args):
            lease_stack.pop()

    class FakeSnapshot:
        def as_dict(self):
            return {"dataset_id": "dataset-test"}

    class FakeBridge:
        def create(self, **_kwargs):
            assert lease_stack
            assert lease_stack[-1].lock_name == "training-snapshot-create"
            assert lease_stack[-1].heartbeat_enabled is False
            return FakeSnapshot()

    monkeypatch.setattr(cli, "_runtime", lambda *_args: (app_config, engine))
    monkeypatch.setattr(cli, "ProcessLease", FakeLease)
    monkeypatch.setattr(cli, "create_training_snapshot_bridge", lambda _cfg: FakeBridge())
    monkeypatch.setattr(cli, "_emit", lambda *_args, **_kwargs: None)

    cli.command_create_training_snapshot(
        profile="quick",
        targets="PM10",
        mirror_manifest=False,
        config=None,
        env_file=None,
    )

    assert [(row.lock_name, row.heartbeat_enabled) for row in leases] == [
        ("training-snapshot-create", False)
    ]


def test_snapshot_training_starts_renewable_lease_only_after_copy(
    engine,
    app_config,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    lease_stack: list[SimpleNamespace] = []
    leases: list[SimpleNamespace] = []
    events: list[str] = []

    class FakeLease:
        def __init__(
            self,
            _engine,
            _config,
            lock_name: str,
            *,
            heartbeat_enabled: bool = True,
        ) -> None:
            self.record = SimpleNamespace(
                lock_name=lock_name,
                heartbeat_enabled=heartbeat_enabled,
            )
            leases.append(self.record)

        def __enter__(self):
            lease_stack.append(self.record)
            events.append(f"enter:{self.record.lock_name}")
            return self

        def __exit__(self, *_args):
            events.append(f"exit:{self.record.lock_name}")
            lease_stack.pop()

    class FakeReporter:
        current_path = "current.json"
        run_path = "run.json"
        event_path = "events.jsonl"

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def start(self, **_kwargs):
            return self

        def complete_stage(self, *_args, **_kwargs) -> None:
            pass

        def finish(self, *_args, **_kwargs) -> None:
            pass

        def fail(self, *_args, **_kwargs) -> None:
            pass

    class FakeSnapshot:
        dataset_id = "dataset-test"
        database_path = app_config.paths.database_path

        def as_dict(self):
            return {
                "dataset_id": self.dataset_id,
                "database_path": str(self.database_path),
                "immutable": True,
            }

    class FakeBridge:
        def resolve(self, _profile: str, _selector: str):
            return None

        def create(self, **_kwargs):
            assert lease_stack
            assert lease_stack[-1].lock_name == "training-snapshot-create"
            assert lease_stack[-1].heartbeat_enabled is False
            events.append("snapshot-created")
            return FakeSnapshot()

        def cleanup(self, **_kwargs) -> None:
            events.append("cleanup")

    class FakeSnapshotEngine:
        def dispose(self) -> None:
            events.append("snapshot-engine-disposed")

    @contextmanager
    def fake_session_scope(_engine):
        yield object()

    def fake_train(*_args, **_kwargs):
        assert lease_stack
        assert lease_stack[-1].lock_name == "snapshot-hourly-training"
        assert lease_stack[-1].heartbeat_enabled is True
        assert "exit:training-snapshot-create" in events
        events.append("training")
        return StageStats()

    monkeypatch.setattr(cli, "_runtime", lambda *_args: (app_config, engine))
    monkeypatch.setattr(cli, "ProcessLease", FakeLease)
    monkeypatch.setattr(cli, "ProgressReporter", FakeReporter)
    monkeypatch.setattr(cli, "create_training_snapshot_bridge", lambda _cfg: FakeBridge())
    monkeypatch.setattr(cli, "create_snapshot_engine", lambda _path: FakeSnapshotEngine())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(cli, "train_hourly_models", fake_train)
    monkeypatch.setattr(cli, "_emit", lambda *_args, **_kwargs: None)

    cli._run_snapshot_training(
        selected="quick",
        targets="PM10",
        snapshot_selector="auto",
        candidate_only=False,
        config=None,
        env_file=None,
    )

    assert [(row.lock_name, row.heartbeat_enabled) for row in leases] == [
        ("training-snapshot-create", False),
        ("snapshot-hourly-training", True),
    ]
    assert events.index("snapshot-created") < events.index(
        "enter:snapshot-hourly-training"
    )
