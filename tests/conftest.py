from __future__ import annotations

import pytest

from detached import ApprovalLoop, Approver, FrozenClock, Item, UNANIMOUS
from detached.adapters import MemoryStore, MemoryTransport, RecordingNotifier


@pytest.fixture
def clock():
    return FrozenClock(start=1_700_000_000)


@pytest.fixture
def transport():
    return MemoryTransport(address="loop@example.test")


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def escalations():
    return []


@pytest.fixture
def make_loop(store, transport, clock, notifier, escalations):
    def _make(policy=UNANIMOUS, **kw):
        return ApprovalLoop("test", store=store, transport=transport, policy=policy,
                            clock=clock, notifier=notifier,
                            on_escalate=escalations.append, **kw)
    return _make


@pytest.fixture
def loop(make_loop):
    return make_loop()


def items(n=3, advisory=()):
    return [Item(n=i, summary=f"item {i}", payload={"i": i}, advisory=(i in advisory))
            for i in range(1, n + 1)]


def approvers(*addresses, channel=True):
    return [Approver(a, role=f"role{i}", channel_id=f"U{i}" if channel else "")
            for i, a in enumerate(addresses, start=1)]
