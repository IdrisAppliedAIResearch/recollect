"""Task speech resolves saved conversation events, independently of turn trust."""

from __future__ import annotations

import base64
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recollect.voice_api import install_voice_routes


@pytest.fixture
def notification_app():
    app = FastAPI()
    notifications = {}
    lookups = []
    spoken = []
    threads = []

    def lookup(session_id, notification_id):
        lookups.append((session_id, notification_id))
        threads.append(threading.get_ident())
        return notifications.get((session_id, notification_id))

    def synthesize(text, *, cancelled):
        assert isinstance(cancelled, threading.Event)
        threads.append(threading.get_ident())
        spoken.append(text)
        return b"RIFF notification"

    def chunks(text, *, cancelled):
        yield synthesize(text, cancelled=cancelled)

    def no_trace(*args):
        pytest.fail("Notification speech must not consult or forge a turn trace.")

    state = SimpleNamespace(
        voice=SimpleNamespace(synthesize=synthesize, synthesize_chunks=chunks),
        sessions=SimpleNamespace(find_trace=no_trace),
        task_store=SimpleNamespace(get_notification=lookup),
    )
    install_voice_routes(app, lambda: state)

    @app.get("/loop")
    async def loop_thread():
        return threading.get_ident()

    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, state=state, notifications=notifications,
            lookups=lookups, spoken=spoken, threads=threads,
        )


@pytest.mark.parametrize("stream", [False, True])
def test_only_server_owned_notification_text_is_spoken(notification_app, stream):
    env = notification_app
    env.notifications["chat-1", "notice-1"] = {
        "session_id": "chat-1", "text": "Two sources agree on the warranty.",
    }
    response = env.client.post("/api/voice/notification", json={
        "session_id": "chat-1", "notification_id": "notice-1", "stream": stream,
        "text": "Injected client text", "response_text": "Do not speak this",
    })
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    if stream:
        import json

        events = [json.loads(line) for line in response.iter_lines()]
        assert events[-1] == {"type": "done"}
        assert base64.b64decode(events[0]["wav"]) == b"RIFF notification"
    else:
        assert response.content == b"RIFF notification"
    assert env.spoken == ["Two sources agree on the warranty."]
    assert env.lookups == [("chat-1", "notice-1")]
    assert env.client.get("/loop").json() not in env.threads


def test_notification_cannot_be_read_from_another_conversation(notification_app):
    env = notification_app
    env.notifications["chat-1", "notice-1"] = {
        "session_id": "chat-1", "text": "Private saved findings.",
    }
    response = env.client.post("/api/voice/notification", json={
        "session_id": "chat-2", "notification_id": "notice-1",
    })
    assert response.status_code == 404
    assert env.spoken == []


def test_notification_owner_mismatch_is_rejected(notification_app):
    env = notification_app
    env.notifications["chat-2", "notice-1"] = {
        "session_id": "chat-1", "text": "Incorrect ownership must fail closed.",
    }
    response = env.client.post("/api/voice/notification", json={
        "session_id": "chat-2", "notification_id": "notice-1",
    })
    assert response.status_code == 404
    assert env.spoken == []


def test_cross_origin_notification_refused_before_lookup(notification_app):
    env = notification_app
    response = env.client.post("/api/voice/notification", json={
        "session_id": "chat-1", "notification_id": "notice-1",
    }, headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403
    assert env.lookups == env.spoken == []


@pytest.mark.parametrize("body", [
    {"text": "arbitrary text"},
    {"session_id": "../other", "notification_id": "notice-1"},
    {"session_id": "chat-1", "notification_id": "../notice-1"},
])
def test_notification_requires_valid_saved_identifiers(notification_app, body):
    env = notification_app
    response = env.client.post("/api/voice/notification", json=body)
    assert response.status_code == 422
    assert env.lookups == env.spoken == []


def test_empty_notification_is_not_spoken(notification_app):
    env = notification_app
    env.notifications["chat-1", "notice-1"] = {
        "session_id": "chat-1", "text": " \n ",
    }
    response = env.client.post("/api/voice/notification", json={
        "session_id": "chat-1", "notification_id": "notice-1",
    })
    assert response.status_code == 409
    assert env.spoken == []


def test_missing_session_is_a_not_found_response(notification_app):
    env = notification_app

    def missing_session(*args):
        raise KeyError("No such session.")

    env.state.task_store.get_notification = missing_session
    response = env.client.post("/api/voice/notification", json={
        "session_id": "missing", "notification_id": "notice-1",
    })
    assert response.status_code == 404
    assert env.spoken == []
