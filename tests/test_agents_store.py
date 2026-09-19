"""The durable-state seam: validated, namespaced, bounded, atomic."""

import json

import pytest

from recollect.agents_store import AgentStore


@pytest.fixture
def store(tmp_path):
    return AgentStore(tmp_path / "agents")


def test_roundtrip_keeps_the_value_and_reports_metadata(store):
    meta = store.put("calendar", "event-1", {"summary": "dentist"})
    assert store.get("calendar", "event-1") == {"summary": "dentist"}
    assert meta["key"] == "event-1" and meta["bytes"] > 0
    listed = store.list("calendar")
    assert [entry["key"] for entry in listed] == ["event-1"]
    assert listed[0]["updated_at"]


def test_absent_namespace_reads_empty_and_absent_entry_raises(store):
    assert store.list("nothing-here") == []
    with pytest.raises(KeyError):
        store.get("nothing-here", "anything")


@pytest.mark.parametrize("namespace", ["", "UPPER", "a/b", "a\\b", "..x",
                                       "a" * 33, "with space", "dot.dot"])
def test_a_namespace_that_could_traverse_or_clash_is_refused(store, namespace):
    with pytest.raises(ValueError):
        store.put(namespace, "safe", 1)
    with pytest.raises(ValueError):
        store.list(namespace)


@pytest.mark.parametrize("key", ["", "with/slash", "..", "..x", "x\\y",
                                 "x" * 65, "ünicode"])
def test_a_key_that_could_escape_its_file_is_refused(store, key):
    with pytest.raises(ValueError):
        store.put("cal", key, 1)
    with pytest.raises(ValueError):
        store.get("cal", key)


def test_an_oversized_value_is_refused(store):
    with pytest.raises(ValueError, match="16384"):
        store.put("cal", "big", "x" * 20_000)


def test_a_non_json_value_is_refused(store):
    with pytest.raises(ValueError, match="JSON"):
        store.put("cal", "bad", {1, 2})


def test_key_and_namespace_quotas_hold(monkeypatch, tmp_path):
    monkeypatch.setattr("recollect.agents_store.MAX_KEYS_PER_NAMESPACE", 2)
    monkeypatch.setattr("recollect.agents_store.MAX_NAMESPACES", 2)
    store = AgentStore(tmp_path / "agents")
    store.put("cal", "a", 1)
    store.put("cal", "b", 2)
    with pytest.raises(ValueError, match="entries"):
        store.put("cal", "c", 3)
    store.put("one", "x", 1)  # cal + one = the two allowed namespaces
    with pytest.raises(ValueError, match="namespaces"):
        store.put("two", "x", 1)
    # Updating an existing entry never counts against the key quota.
    store.put("cal", "b", 22)
    assert store.get("cal", "b") == 22


def test_a_malformed_entry_fails_loudly(store, tmp_path):
    path = tmp_path / "agents" / "cal"
    path.mkdir(parents=True)
    (path / "rot.json").write_text('{"schema": 99, "data": 1}', "utf-8")
    with pytest.raises(ValueError, match="malformed"):
        store.get("cal", "rot")
    with pytest.raises(ValueError, match="malformed"):
        store.list("cal")


def test_delete_removes_once(store):
    store.put("cal", "gone", 1)
    store.delete("cal", "gone")
    with pytest.raises(KeyError):
        store.delete("cal", "gone")


def test_a_put_leaves_no_staging_file(store, tmp_path):
    store.put("cal", "clean", {"ok": True})
    leftovers = [p.name for p in (tmp_path / "agents" / "cal").iterdir()
                 if not p.name.endswith(".json")]
    assert leftovers == []
    # And the file itself is a versioned record.
    record = json.loads((tmp_path / "agents" / "cal" / "clean.json")
                        .read_text(encoding="utf-8"))
    assert record["schema"] == 1
