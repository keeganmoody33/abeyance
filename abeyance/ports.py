"""The five seams the library talks to the world through.

These are `typing.Protocol`s, not base classes, so an adapter is anything with the right
methods — including your existing mail wrapper. Nothing here imports a vendor SDK; the
concrete adapters in `abeyance.adapters` do, each behind its own optional extra, which is why
the core installs with zero dependencies.

Why these five and no more: a loop needs somewhere to *keep* state while no process is running
(Store), a way to *ask and hear back* (Transport), an optional way to *poke someone who has
gone quiet* (Notifier), a *clock* it does not own so tests can drive a seven-day expiry without
waiting seven days — and, for the case layer, a way to *start an isolated worker* and later ask
what became of it (Runner).
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, runtime_checkable

from .models import Proposal, Reply, Sent


@runtime_checkable
class Store(Protocol):
    """Durable state, keyed by (kind, key).

    The whole library's continuity lives here. Two properties matter more than speed:

    **It must outlive the process.** An in-memory store is for tests only.

    **It should be shared by every host that runs the loop.** Per-host state (a JSON file on
    one machine) is fine while exactly one machine runs the loop and quietly wrong the moment
    a second one has a copy: each host reads its own cursors, and a machine that stopped
    running the loop keeps a frozen snapshot it cannot distinguish from "nothing happened".
    Prefer `PostgresStore` anywhere a laptop and a scheduled worker might both run this.
    """

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]: ...

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None: ...

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        """Every doc of a kind, as {key: doc}. One round trip, not N — the poll tick calls
        this on every run and an N+1 here is the difference between a cheap gate and an
        expensive one."""
        ...

    def delete(self, kind: str, key: str) -> None: ...


@runtime_checkable
class Transport(Protocol):
    """Ask a question, hear the answers, know who spoke.

    `fetch_replies` carries the one rule that is easy to get wrong and expensive to get wrong:
    **exclude our own messages by sender, not only by id.** Anchoring on ids alone means a
    message we sent but failed to record — a crash between the send and the state write —
    comes back looking like an inbound reply, and an apply tick then reads our own proposal
    text as somebody's approval.
    """

    @property
    def address(self) -> str:
        """Our own identity, as replies will show it. Used to drop our own messages."""
        ...

    def send(self, to: Sequence[str], subject: str, body: str,
             cc: Sequence[str] = (), thread_id: Optional[str] = None,
             in_reply_to: Optional[str] = None) -> Sent:
        """Send one message. Passing `thread_id`/`in_reply_to` continues an existing
        conversation; omitting both starts a new one."""
        ...

    def fetch_replies(self, thread_id: str, exclude_ids: Iterable[str] = (),
                      after_epoch: int = 0) -> List[Reply]:
        """Inbound messages on a thread, oldest first, each attributed to a sender."""
        ...


@runtime_checkable
class Notifier(Protocol):
    """Out-of-band poke for an approver who has gone quiet.

    Separate from Transport on purpose: the nudge should arrive somewhere the person actually
    looks, which is usually not the mailbox they are already ignoring.
    """

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> int:
        """Unix seconds."""
        ...


class SystemClock:
    """The real one."""

    def now(self) -> int:
        return int(time.time())


class FrozenClock:
    """A clock you drive by hand. Lets a test walk a proposal through a 7-day expiry and a
    72-hour second nudge in microseconds, which is the only way those paths get covered."""

    def __init__(self, start: int = 1_700_000_000) -> None:
        self._t = int(start)

    def now(self) -> int:
        return self._t

    def advance(self, *, seconds: int = 0, minutes: int = 0, hours: int = 0, days: int = 0) -> int:
        self._t += seconds + minutes * 60 + hours * 3600 + days * 86400
        return self._t


@runtime_checkable
class Renderer(Protocol):
    """Turns a proposal into the words a human reads. Swap it to change tone, depth, or
    format without touching the state machine."""

    def subject(self, proposal: Proposal) -> str: ...

    def body(self, proposal: Proposal, *, expire_days: float) -> str: ...


# --------------------------------------------------------------------------- the fifth seam


class RunState(str, Enum):
    """What a started worker is doing now. Four states, because the interesting one is `GONE`.

    A dispatcher that can only ask "running or not" cannot distinguish a worker still doing its
    job from a worker that never booted, and those need opposite responses. `GONE` — the
    platform has no record of it — is the state that makes a lost dispatch detectable instead
    of indistinguishable from patience.
    """

    RUNNING = "running"
    EXITED = "exited"
    """Finished and gone away cleanly. Note this says nothing about whether it *contributed* —
    a worker that exits 0 having written nothing is the crash-after-boot case, and the
    dispatcher treats a satisfied request, not a clean exit, as success."""

    FAILED = "failed"
    GONE = "gone"
    """No record of it. Either it never started or the platform reaped it."""


@runtime_checkable
class Runner(Protocol):
    """Start an isolated worker, and later ask what became of it.

    The fifth seam, and the one that makes a case's contributors disposable. Deliberately
    primitive — image, command, environment — so the runner knows nothing about cases,
    contributions, or authority. That is what lets the same dispatcher drive a Fly machine, a
    local subprocess, and a fake in a test.

    Two things a Runner is explicitly NOT responsible for, because owning them is how this
    would turn into a scheduler:

      **Retries.** The dispatcher owns them, because "should we try again" is a question about
      the case, not about the platform.

      **Delivering results.** A worker writes its contribution to the store itself. The runner
      never carries a payload back, so a worker that finishes after the dispatcher process has
      exited still counts — which is the entire point.
    """

    @property
    def name(self) -> str:
        """Short identifier for logs and provenance — "fly", "local"."""
        ...

    def start(self, *, image: str, cmd: Sequence[str], env: Dict[str, str],
              app: str = "", entrypoint: Sequence[str] = (), label: str = "",
              guest: Optional[Dict[str, Any]] = None, timeout_seconds: int = 600) -> str:
        """Start one worker and return an opaque reference. Must not block until it finishes."""
        ...

    def state(self, ref: str) -> RunState:
        """What became of the worker with this reference."""
        ...

    def stop(self, ref: str) -> None:
        """Best-effort termination. Called on a worker that blew its declared timeout."""
        ...
