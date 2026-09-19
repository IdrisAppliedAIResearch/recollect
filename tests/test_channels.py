"""The send seam: hub targets, one raw ntfy POST, deliverer composition."""

import json

import httpx
import pytest

from recollect.channels import ChannelError, ChannelHub, ntfy
from recollect.selfmod.service import notice_delivery


class FakeChannel:
    """A channel module stand-in: records sends, may be made to fail."""

    def __init__(self, name="ch", fail=""):
        self.name = name
        self.sent = []
        self._fail = fail

    async def send(self, config, text):
        if self._fail:
            raise ChannelError(self._fail)
        self.sent.append(text)


def hub_with(tmp_path, **configs):
    """A hub over credential files written as ``name={...}`` kwargs."""
    registry = {}
    for name, config in configs.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(config), "utf-8")
        registry[name] = FakeChannel(name)
    return ChannelHub(tmp_path, registry=registry), registry


def test_configured_reports_names_and_never_the_values(tmp_path):
    (tmp_path / "ntfy.json").write_text(json.dumps(
        {"url": "https://ntfy.sh/secret-topic", "default": True}))
    hub = ChannelHub(tmp_path, registry={"ntfy": ntfy})
    listed = hub.configured()
    assert listed == [{"name": "ntfy", "default": True}]
    assert "secret-topic" not in json.dumps(listed)


def test_unconfigured_and_malformed_files_are_simply_absent(tmp_path):
    (tmp_path / "ntfy.json").write_text("not json", "utf-8")
    hub = ChannelHub(tmp_path, registry={"ntfy": ntfy})
    assert hub.configured() == []


def test_targets_name_then_default_then_everyone(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"url": "x"}), "utf-8")
    (tmp_path / "b.json").write_text(json.dumps({"default": True}), "utf-8")
    hub = ChannelHub(tmp_path, registry={"a": ntfy, "b": ntfy})
    assert hub.targets("a") == ["a"]
    assert hub.targets() == ["b"]
    with pytest.raises(ValueError, match="not configured"):
        hub.targets("missing")
    (tmp_path / "b.json").write_text(json.dumps({}), "utf-8")
    assert sorted(hub.targets()) == ["a", "b"]


def test_a_payload_channel_name_cannot_probe_the_filesystem(tmp_path):
    # The channel name comes from a job payload, which is not trusted: a
    # valid config file outside the store must be indistinguishable from
    # an absent one, in both the answer and the recorded reason.
    store = tmp_path / "channels"
    store.mkdir()
    (tmp_path / "secret.json").write_text(
        json.dumps({"url": "https://ntfy.sh/secret"}), "utf-8")
    (store / "ntfy.json").write_text(json.dumps({"url": "https://x"}),
                                     "utf-8")
    hub = ChannelHub(store, registry={"ntfy": ntfy})
    for name in ("../secret", "a/b", "ntfy.json", "missing"):
        with pytest.raises(ValueError, match="not configured"):
            hub.targets(name)


async def test_ntfy_posts_the_bare_text_as_the_message():
    calls = []

    def handler(request):
        calls.append(request.content)
        return httpx.Response(204)

    await ntfy.send({"url": "https://ntfy.sh/topic"}, "call mom",
                    transport=httpx.MockTransport(handler))
    assert calls == [b"call mom"]


async def test_ntfy_refusals_and_unreachable_hosts_become_channel_errors():
    def refuses(request):
        return httpx.Response(500)

    with pytest.raises(ChannelError, match="refused"):
        await ntfy.send({"url": "https://ntfy.sh/topic"}, "text",
                        transport=httpx.MockTransport(refuses))

    def explode(request):
        raise httpx.ConnectError("down")

    with pytest.raises(ChannelError, match="reached"):
        await ntfy.send({"url": "https://ntfy.sh/topic"}, "text",
                        transport=httpx.MockTransport(explode))


async def test_ntfy_config_without_a_usable_url_is_refused():
    with pytest.raises(ChannelError, match="https"):
        await ntfy.send({}, "text")


async def test_hub_send_requires_a_configured_known_channel(tmp_path):
    hub = ChannelHub(tmp_path, registry={"ntfy": ntfy})
    with pytest.raises(ChannelError, match="no such channel"):
        await hub.send("smtp", "x")
    with pytest.raises(ChannelError, match="not configured"):
        await hub.send("ntfy", "x")


def _job(**payload):
    return {"job_id": "j1", "payload": {
        "session_id": "s", "task_id": "t", "text": "call mom", **payload}}


async def _deliver(hub, job):
    posted = []

    def notify(session_id, task_id, message_id, text, kind="progress"):
        posted.append((session_id, task_id, text, kind))

    await notice_delivery(notify, hub)(job)
    return posted


async def test_the_notice_posts_then_every_channel_receives_it(tmp_path):
    hub, channels = hub_with(tmp_path,
                             one={"url": "u"}, two={"url": "u"})
    posted = await _deliver(hub, _job())
    assert posted == [("s", "t", "call mom", "reminder")]
    assert channels["one"].sent == ["call mom"]
    assert channels["two"].sent == ["call mom"]


async def test_a_named_channel_selects_only_it(tmp_path):
    hub, channels = hub_with(tmp_path, one={"url": "u"}, two={"url": "u"})
    await _deliver(hub, _job(channel="one"))
    assert channels["one"].sent == ["call mom"]
    assert channels["two"].sent == []


async def test_the_notice_posts_even_when_a_channel_send_fails(tmp_path):
    failing = FakeChannel(fail="ntfy refused the message (HTTP 500)")
    hub = ChannelHub(tmp_path, registry={"one": failing})
    (tmp_path / "one.json").write_text('{"url": "u"}', "utf-8")
    posted = []

    def notify(session_id, task_id, message_id, text, kind="progress"):
        posted.append(kind)

    with pytest.raises(RuntimeError, match="ntfy refused"):
        await notice_delivery(notify, hub)(_job())
    assert posted == ["reminder"]


async def test_a_bad_payload_neither_notifies_nor_sends(tmp_path):
    hub, channels = hub_with(tmp_path, one={"url": "u"})
    with pytest.raises(ValueError, match="task_id"):
        await _deliver(hub, {"job_id": "j", "payload":
                             {"session_id": "s", "text": "x"}})
    assert channels["one"].sent == []


async def test_a_nonstring_channel_fails_validation(tmp_path):
    hub, _ = hub_with(tmp_path, one={"url": "u"})
    with pytest.raises(ValueError, match="channel"):
        await _deliver(hub, _job(channel=7))
