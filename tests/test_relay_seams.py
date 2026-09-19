"""The store and channel endpoints answer relay key-holders, nothing more."""

import httpx

from recollect.agents_store import AgentStore
from recollect.channels import ChannelHub
from recollect.connections import ConnectionService

_UNSET = object()


class RecordingChannel:
    def __init__(self, fail=""):
        self.sent = []
        self._fail = fail

    async def send(self, config, text):
        if self._fail:
            raise RuntimeError(self._fail)
        self.sent.append(text)


def wire(tmp_path, *, store=True, channels=_UNSET):
    """A ConnectionService with the seams wired (or deliberately not)."""
    if channels is _UNSET:
        root = tmp_path / "channels"
        root.mkdir(exist_ok=True)
        (root / "ntfy.json").write_text('{"url": "https://ntfy.sh/t"}',
                                        "utf-8")
        channels = ChannelHub(root, registry={"ntfy": RecordingChannel()})
    return ConnectionService(
        store=AgentStore(tmp_path / "agents") if store else None,
        channels=channels)


def http_client(service):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=service.app),
        base_url="http://relay",
        headers={"Authorization": f"Bearer {service.key}"})


async def test_the_store_endpoints_round_trip(tmp_path):
    async with http_client(wire(tmp_path)) as http:
        put = await http.put("/agents/calendar/entries/event-1",
                             json={"data": {"summary": "dentist"}})
        assert put.status_code == 200 and put.json()["key"] == "event-1"
        got = await http.get("/agents/calendar/entries/event-1")
        assert got.json() == {"data": {"summary": "dentist"}}
        listed = await http.get("/agents/calendar/entries")
        assert [entry["key"] for entry in listed.json()] == ["event-1"]
        namespaces = await http.get("/agents")
        assert namespaces.json() == ["calendar"]
        gone = await http.delete("/agents/calendar/entries/event-1")
        assert gone.json() == {"deleted": True}
        assert (await http.get("/agents/calendar/entries/event-1")
                ).status_code == 404


async def test_store_input_errors_are_400_not_500(tmp_path):
    async with http_client(wire(tmp_path)) as http:
        assert (await http.put("/agents/BAD NS/entries/k",
                               json={"data": 1})).status_code == 400
        assert (await http.put("/agents/cal/entries/..x",
                               json={"data": 1})).status_code == 400
        assert (await http.put("/agents/cal/entries/k",
                               json={"nope": 1})).status_code == 400
        assert (await http.put("/agents/cal/entries/k",
                               json={"data": "x" * 20_000})
                ).status_code == 400


async def test_channels_list_hides_urls_and_test_sends(tmp_path):
    channel = RecordingChannel()
    root = tmp_path / "ch"
    root.mkdir()
    (root / "one.json").write_text('{"url": "https://x/t"}', "utf-8")
    hub = ChannelHub(root, registry={"one": channel})
    async with http_client(wire(tmp_path, channels=hub)) as http:
        listed = await http.get("/channels")
        assert listed.json() == [{"name": "one", "default": False}]
        assert "https://x/t" not in listed.text
        sent = await http.post("/channels/one/test", json={"text": "hello"})
        assert sent.json() == {"sent": True}
        assert channel.sent == ["hello"]


async def test_channel_test_answers_missing_text_and_failing_send(tmp_path):
    broken = RecordingChannel(fail="ntfy refused the message (HTTP 500)")
    root = tmp_path / "ch"
    root.mkdir()
    (root / "one.json").write_text('{"url": "u"}', "utf-8")
    hub = ChannelHub(root, registry={"one": broken})
    async with http_client(wire(tmp_path, channels=hub)) as http:
        assert (await http.post("/channels/one/test",
                                json={})).status_code == 400
        assert (await http.post("/channels/one/test",
                                json={"text": "  "})
                ).status_code == 400
        refused = await http.post("/channels/one/test", json={"text": "hi"})
        assert refused.status_code == 503
        assert "ntfy refused" in refused.json()["detail"]


async def test_unwired_seams_answer_empty_not_error():
    async with http_client(ConnectionService()) as http:
        assert (await http.get("/agents")).json() == []
        assert (await http.get("/agents/cal/entries")).json() == []
        assert (await http.get("/channels")).json() == []
        assert (await http.put("/agents/cal/entries/k",
                               json={"data": 1})).status_code == 404
        assert (await http.post("/channels/one/test",
                                json={"text": "x"})
                ).status_code == 404


async def test_the_key_gates_every_new_endpoint(tmp_path):
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=wire(tmp_path).app),
            base_url="http://relay") as http:
        assert (await http.get("/agents")).status_code == 401
        assert (await http.get("/channels")).status_code == 401
        assert (await http.put("/agents/a/entries/k",
                               json={"data": 1})).status_code == 401
