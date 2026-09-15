"""Role inference is pinned to the modifier lane on three-slot profiles."""

import pytest

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
