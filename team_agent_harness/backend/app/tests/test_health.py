import sqlite3

from fastapi.testclient import TestClient
import pytest

from app import api as api_module
from app.api import create_harness_state
from app.core.storage import SQLiteStorage
from app.main import app, create_app


def test_health_returns_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "worker": "running"}


def test_health_returns_degraded_when_worker_stops(tmp_path) -> None:
    local_app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )

    with TestClient(local_app) as client:
        assert local_app.state.harness.run_worker.stop() is True

        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "worker": "stopped"}


def test_openapi_available() -> None:
    client = TestClient(app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Team Agent Harness"


def test_state_does_not_close_storage_while_worker_is_still_running(tmp_path, monkeypatch) -> None:
    local_app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    state = local_app.state.harness
    storage_closed = False

    monkeypatch.setattr(state.run_worker, "stop", lambda: False)

    def close_storage() -> None:
        nonlocal storage_closed
        storage_closed = True

    monkeypatch.setattr(state.storage, "close", close_storage)

    state.close()

    assert storage_closed is False


def test_state_initialization_closes_storage_when_configuration_loading_fails(tmp_path, monkeypatch) -> None:
    storage = SQLiteStorage(tmp_path / "harness.sqlite3", check_same_thread=False)
    monkeypatch.setattr(api_module, "SQLiteStorage", lambda *args, **kwargs: storage)

    def fail_model_routing(_config_root):
        raise RuntimeError("injected routing failure")

    monkeypatch.setattr(api_module, "_load_model_routing_for_config_root", fail_model_routing)

    with pytest.raises(RuntimeError, match="injected routing failure"):
        create_harness_state(
            tmp_path / "harness.sqlite3",
            tmp_path / "artifacts",
            config_root=tmp_path,
        )

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        storage.conn.execute("SELECT 1")


def test_lifespan_closes_state_when_startup_recovery_fails(tmp_path, monkeypatch) -> None:
    local_app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    state = local_app.state.harness
    original_close = state.close
    state_closed = False

    def fail_start() -> None:
        raise RuntimeError("injected recovery failure")

    def close_state() -> None:
        nonlocal state_closed
        state_closed = True
        original_close()

    monkeypatch.setattr(state, "start", fail_start)
    monkeypatch.setattr(state, "close", close_state)

    with pytest.raises(RuntimeError, match="injected recovery failure"):
        with TestClient(local_app):
            pass

    assert state_closed is True
