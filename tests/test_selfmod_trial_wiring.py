"""Trial wiring prerequisites: pinned role lane and a substitutable app state."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from recollect.api import create_app
from recollect.config import RecollectConfig
from recollect.selfmod.roles import model_payload
from tests.test_selfmod_roles import settings


def test_role_inference_can_be_pinned_to_the_modifier_slot():
    context = {"role": "plan", "stage": "plan", "request_id": "r"}
    assert "id_slot" not in model_payload(context, settings())
    pinned = settings(slot=2)
    assert model_payload(context, pinned)["id_slot"] == 2
    assert pinned.identity != settings().identity
    assert settings().identity == settings().identity
    for bad in (-1, 64, True, "2"):
        with pytest.raises(ValueError, match="slot"):
            settings(slot=bad)


def test_app_serves_a_trial_supplied_state(tmp_path):
    config = RecollectConfig(embedding_model_path=tmp_path / "e.gguf",
                             data_dir=tmp_path / "var")
    closed = []

    async def close(name):
        closed.append(name)

    def factory(received):
        assert received is config
        return SimpleNamespace(
            config=received,
            embedder=SimpleNamespace(warm_up=lambda: {"ok": True}, stats={}),
            sandboxes=SimpleNamespace(close_all=lambda: close("sandboxes")),
            generator=SimpleNamespace(aclose=lambda: close("generator")),
            web_client=SimpleNamespace(aclose=lambda: close("web")),
        )

    app = create_app(config, serve_ui=False, state_factory=factory)
    with TestClient(app):
        assert app.state.recollect.embedder_health == {"ok": True}
    assert closed == ["sandboxes", "generator", "web"]
