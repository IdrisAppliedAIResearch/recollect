"""Untrusted IDs cannot turn session APIs into filesystem operations elsewhere."""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from recollect.api import create_app
from recollect.config import RecollectConfig
from recollect.session import SessionManager
from tests.conftest import FakeEmbedder


@pytest.fixture
def manager(tmp_path):
    return SessionManager(RecollectConfig(
        embedding_model_path=tmp_path / "unused.gguf", data_dir=tmp_path / "data",
        aspect_enabled=False, subagent_enabled=False,
    ), FakeEmbedder())


@pytest.mark.parametrize("identifier", [
    "../outside", r"..\..\outside", "/absolute", r"C:\outside", "..", "*", "a?",
    "AUX", "com1", "trailing.", " leading", "bad\x00id", "x" * 129,
])
def test_storage_rejects_path_glob_and_device_identifiers(manager, identifier):
    for operation in (
        manager.get_session, manager.open_store, manager.list_turns,
        manager.config.session_dir, manager.config.traces_dir,
        manager.config.store_path, manager.find_trace,
    ):
        with pytest.raises(ValueError, match="identifier"):
            operation(identifier)
    assert list(manager.config.sessions_dir.iterdir()) == []


def test_episode_reads_do_not_create_unknown_or_empty_stores(manager):
    with pytest.raises(KeyError):
        manager.episode("missing", "1")
    assert not manager.config.session_dir("missing").exists()
    session = manager.create_session("An ordinary session")
    assert manager.episode(session.session_id, "1") is None
    assert not manager.config.store_path(session.session_id).exists()


def test_resolved_symlink_destination_is_checked_for_each_storage_file(
    manager, tmp_path, monkeypatch,
):
    # Model a redirected file resolution without requiring Windows symlink rights.
    path_type = type(tmp_path)
    original = path_type.resolve
    outside = tmp_path / "outside"
    redirected = manager.config.sessions_dir / "safe" / "episodes.sqlite"

    def resolve(path, *args, **kwargs):
        return outside if path == redirected else original(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "resolve", resolve)
    with pytest.raises(ValueError, match="leaves"):
        manager.config.store_path("safe")
    assert not outside.exists()


def test_metadata_cannot_redirect_later_writes_to_another_session(manager):
    original = manager.create_session()
    path = manager.config.session_file(original.session_id, "session.json")
    changed = original.model_copy(update={"session_id": "another"})
    path.write_text(changed.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        manager.get_session(original.session_id)
    assert manager.list_sessions() == []


async def test_api_rejects_traversal_before_any_store_is_created(manager):
    app = create_app(manager.config, serve_ui=False)
    app.state.recollect = SimpleNamespace(sessions=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
    ) as client:
        escaped = quote(r"..\..\outside", safe="")
        response = await client.get(f"/api/sessions/{escaped}/episodes/1")
        assert response.status_code == 400
        response = await client.post("/api/chat", json={
            "session_id": r"..\..\outside", "message": "hello",
        })
        assert response.status_code == 422
        assert (await client.get("/api/sessions/missing/episodes/1")).status_code == 404
    assert list(manager.config.sessions_dir.iterdir()) == []


async def test_session_title_limits_preserve_normal_creation(manager):
    app = create_app(manager.config, serve_ui=False)
    app.state.recollect = SimpleNamespace(sessions=manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080",
    ) as client:
        rejected = await client.post("/api/sessions", json={"title": "a" * 513})
        assert rejected.status_code == 422
        assert manager.list_sessions() == []
        response = await client.post("/api/sessions", json={"title": "My conversation"})
        assert response.status_code == 200
        assert response.json()["title"] == "My conversation"
