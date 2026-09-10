import logging
from unittest.mock import patch

from dooers.agents.server.storage.object_index import ObjectIndex


def test_record_then_entries_filters_by_prefix():
    store = {"data": ({}, 0)}

    def _read(b, p):
        return store["data"]

    def _write(b, p, d, g):
        store["data"] = (d, store["data"][1] + 1)
        return True

    with (
        patch("dooers.agents.server.storage.object_index.gcs.read_json_with_generation", _read),
        patch("dooers.agents.server.storage.object_index.gcs.write_json_if_generation", _write),
    ):
        idx = ObjectIndex("b", "agents/o1/.dooers/object-index.json")
        idx.record("rag/a.txt", 3, "text/plain")
        idx.record("chat/b.txt", 4, None)
        keys = [e["key"] for e in idx.entries("rag/")]
    assert keys == ["rag/a.txt"]


def test_remove_drops_the_key():
    store = {"data": ({"rag/a.txt": {"size": 3, "content_type": None, "updated_at": "t"}}, 1)}

    def _read(b, p):
        return store["data"]

    def _write(b, p, d, g):
        store["data"] = (d, store["data"][1] + 1)
        return True

    with (
        patch("dooers.agents.server.storage.object_index.gcs.read_json_with_generation", _read),
        patch("dooers.agents.server.storage.object_index.gcs.write_json_if_generation", _write),
    ):
        idx = ObjectIndex("b", "p")
        idx.remove("rag/a.txt")
    assert store["data"][0] == {}


def test_record_retries_on_precondition_then_succeeds():
    calls = {"n": 0}

    def _read(b, p):
        return ({}, calls["n"])

    def _write(b, p, d, g):
        calls["n"] += 1
        return calls["n"] >= 2  # first write fails, second succeeds

    with (
        patch("dooers.agents.server.storage.object_index.gcs.read_json_with_generation", _read),
        patch("dooers.agents.server.storage.object_index.gcs.write_json_if_generation", _write),
    ):
        ObjectIndex("b", "p").record("k", 1, None)
    assert calls["n"] == 2


def test_mutate_swallows_read_error_and_logs(caplog):
    # gcs.read_json_with_generation deliberately raises on genuine GCS errors (so a
    # transient failure isn't mistaken for "index absent") — _mutate must catch that
    # and give up quietly rather than let it propagate out of record()/remove().
    def _read(b, p):
        raise RuntimeError("boom")

    with (
        patch("dooers.agents.server.storage.object_index.gcs.read_json_with_generation", _read),
        caplog.at_level(logging.WARNING, logger="dooers.agents.server.storage.object_index"),
    ):
        ObjectIndex("b", "p").record("k", 1, None)  # must not raise
    assert any("boom" in r.message for r in caplog.records)


def test_entries_skips_corrupt_non_dict_values():
    store = {
        "data": (
            {
                "good/a.txt": {"size": 1, "content_type": None, "updated_at": "t"},
                "bad/b.txt": "not-a-dict",
            },
            1,
        )
    }

    def _read(b, p):
        return store["data"]

    with patch("dooers.agents.server.storage.object_index.gcs.read_json_with_generation", _read):
        keys = [e["key"] for e in ObjectIndex("b", "p").entries()]
    assert keys == ["good/a.txt"]
