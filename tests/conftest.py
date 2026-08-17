from __future__ import annotations

import pytest

from abeyance import (ApprovalLoop, Approver, Capability, CapabilityRegistry, CaseLoop,
                      CasePolicy, ContributionKind, FrozenClock, Item, UNANIMOUS)
from abeyance.adapters import (MemoryRunner, MemoryStore, MemoryTransport, RecordingNotifier)


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


# --------------------------------------------------------------------------- case layer


@pytest.fixture
def runner():
    return MemoryRunner()


@pytest.fixture
def registry():
    """Three capabilities covering the shapes that matter: an evidence gatherer, a model that
    produces opinions, and a second evidence gatherer that only a rule ever asks for."""
    return CapabilityRegistry([
        Capability(name="db-evidence", image="postgres:16-alpine",
                   produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="workers-readonly", timeout_seconds=120),
        Capability(name="fit-scorer", image="python:3.12-slim",
                   produces=("fit-score",), emits=ContributionKind.RECOMMENDATION,
                   reach=("public-internet",), app="workers-model", timeout_seconds=300),
        Capability(name="deep-check", image="postgres:16-alpine",
                   produces=("integrity-deep-check",), emits=ContributionKind.EVIDENCE,
                   reach=("db-read",), app="workers-readonly", timeout_seconds=180),
    ])


@pytest.fixture
def make_cases(store, registry, runner, clock, escalations, loop):
    def _make(rules=(), policy=None, with_approval=True, with_runner=True, **kw):
        return CaseLoop("test", store=store, registry=registry, rules=list(rules),
                        policy=policy or CasePolicy(),
                        runner=runner if with_runner else None,
                        approval=loop if with_approval else None,
                        clock=clock, on_escalate=escalations.append, **kw)
    return _make


@pytest.fixture
def cases(make_cases):
    return make_cases()
