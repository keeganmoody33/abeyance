"""Store contract parity.

Both implementations have to behave identically or the choice of store silently changes the
behaviour of the loop — and the store is the one component people swap late, in production,
under time pressure.
"""
from __future__ import annotations

import json

import pytest

from detached import Approver, Item, Proposal, Status
from detached.adapters import JSONFileStore, MemoryStore


@pytest.fixture(params=["memory", "json"])
def store(request, tmp_path):
    return MemoryStore() if request.param == "memory" else JSONFileStore(tmp_path / "state")


def test_missing_keys_return_none(store):
    assert store.get("k", "nope") is None
    assert store.items("k") == {}


def test_round_trip(store):
    store.put("k", "a", {"x": 1, "nested": {"y": [1, 2]}})
    assert store.get("k", "a") == {"x": 1, "nested": {"y": [1, 2]}}


def test_kinds_are_isolated(store):
    """Two loops over one store must not see each other's proposals."""
    store.put("loop-a:proposal", "1", {"who": "a"})
    store.put("loop-b:proposal", "1", {"who": "b"})
    assert store.get("loop-a:proposal", "1")["who"] == "a"
    assert list(store.items("loop-b:proposal")) == ["1"]


def test_put_overwrites(store):
    store.put("k", "a", {"v": 1})
    store.put("k", "a", {"v": 2})
    assert store.get("k", "a") == {"v": 2}


def test_delete(store):
    store.put("k", "a", {"v": 1})
    store.delete("k", "a")
    assert store.get("k", "a") is None
    store.delete("k", "a")  # deleting twice is not an error


def test_returned_docs_are_copies(store):
    """A caller mutating what it read must not silently mutate the store — that turns an
    abandoned run into a committed one."""
    store.put("k", "a", {"list": [1]})
    got = store.get("k", "a")
    got["list"].append(2)
    assert store.get("k", "a")["list"] == [1]


def test_keys_with_awkward_characters(store):
    """Ids come from a transport, so they can contain anything — Gmail thread ids, RFC-5322
    message ids with `<`, `@` and `/`."""
    key = "<CAF=abc/123+xyz@mail.example.com>"
    store.put("k", key, {"ok": True})
    assert store.get("k", key) == {"ok": True}


def test_a_proposal_survives_the_round_trip(store):
    """The real contract: everything needed to resume, with no live process in between."""
    p = Proposal(
        id="t1", subject_key="acme", track="B", subject="[acme] 2 items",
        items=[Item(n=1, summary="one", payload={"k": "v"}),
               Item(n=2, summary="two", advisory=True)],
        approvers={"a@x": Approver("a@x", role="csm", approved=[1], replied_at="2026-01-01"),
                   "b@x": Approver("b@x", role="eng")},
        status=Status.PARTIALLY_APPROVED, sent_epoch=1, last_activity_epoch=2, turns=2,
        seen_reply_ids=["r1"], context={"anything": [1, 2]})
    store.put("k", p.id, p.to_doc())

    back = Proposal.from_doc(store.get("k", p.id))
    assert back.to_doc() == p.to_doc()
    assert back.status is Status.PARTIALLY_APPROVED
    assert back.approver("a@x").approved == [1]
    assert back.items[1].advisory is True


def test_json_store_writes_are_atomic(tmp_path):
    """A half-written proposal after a crash loses the approvals recorded in it, and those are
    the one thing in the system that cannot be regenerated."""
    s = JSONFileStore(tmp_path / "state")
    s.put("k", "a", {"v": 1})
    files = list((tmp_path / "state" / "k").iterdir())
    assert [f.suffix for f in files] == [".json"], "no temp files left behind"
    assert json.loads(files[0].read_text()) == {"v": 1}


def test_json_store_ignores_a_corrupt_file(tmp_path):
    """One unreadable document must not take down the poll tick for every other proposal."""
    s = JSONFileStore(tmp_path / "state")
    s.put("k", "good", {"v": 1})
    (tmp_path / "state" / "k" / "bad.json").write_text("{not json")
    assert s.get("k", "bad") is None
    assert list(s.items("k")) == ["good"]
