from __future__ import annotations

import tempfile
from pathlib import Path

import smog_ai.storage.local as local_storage_module
from smog_ai.storage.local import LocalObjectStore


def test_local_object_store_uses_short_atomic_temp_name_for_windows_max_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A valid target path must not fail because its temporary name is longer.

    The former implementation prefixed the temporary file with the complete
    target filename.  In a realistic isolated-test directory on Windows, the
    target stayed below 260 characters while the temporary path crossed that
    limit and ``mkstemp`` raised ``FileNotFoundError``.
    """

    key = (
        "metrics/data-validation/weather_measurements/"
        "report=20260803T181913Z-failed-attempt-weather_measurements.json"
    )
    key_parent = Path(*key.split("/")[:-1])
    filename = key.rsplit("/", 1)[-1]

    # Pad the root so that the final object remains valid under classic
    # MAX_PATH, while the historical long prefix would exceed it.
    base_parent_length = len(str(tmp_path / key_parent))
    desired_parent_length = 190
    padding_length = max(8, desired_parent_length - base_parent_length - 1)
    root = tmp_path / ("p" * padding_length)
    store = LocalObjectStore(root)
    target = store._path(key)  # noqa: SLF001 - regression test of path layout

    historical_temp_name = f".{filename}.abcdefgh.tmp"
    historical_temp_path = target.parent / historical_temp_name
    assert len(str(target)) < 260
    assert len(str(historical_temp_path)) > 260

    original_mkstemp = tempfile.mkstemp
    captured: dict[str, object] = {}

    def max_path_guarded_mkstemp(*, prefix: str, suffix: str, dir: str):
        candidate = Path(dir) / f"{prefix}abcdefgh{suffix}"
        captured.update(prefix=prefix, suffix=suffix, directory=dir, candidate=candidate)
        if len(str(candidate)) > 260:
            raise FileNotFoundError(2, "simulated classic Windows MAX_PATH", str(candidate))
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(local_storage_module.tempfile, "mkstemp", max_path_guarded_mkstemp)

    written = store.put_bytes(key, b'{"valid": true}', immutable=True)

    assert written.key == key
    assert store.get_bytes(key) == b'{"valid": true}'
    assert captured["prefix"] == ".sai-"
    assert len(str(captured["candidate"])) < 260


def test_local_object_store_atomic_overwrite_leaves_no_temp_files(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    store.put_bytes("nested/result.json", b"first")
    store.put_bytes("nested/result.json", b"second")

    assert store.get_bytes("nested/result.json") == b"second"
    assert not list((tmp_path / "objects").rglob(".sai-*.tmp"))
