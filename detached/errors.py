"""Errors, grouped by which way each one fails.

The distinction this library cares about: a **misconfiguration** should stop the run loudly
before anything is sent, and a **runtime disagreement with reality** (an approver who is not
on the thread, a proposal already executed) should be reported and survivable. Nothing here
is raised to signal a normal outcome — a blocked apply and a deadlock are return values, not
exceptions, because they happen on a healthy day.
"""
from __future__ import annotations


class DetachedError(Exception):
    """Base class, so a caller can catch everything from this library in one clause."""


# --------------------------------------------------------------------------- configuration


class ConfigurationError(DetachedError):
    """The loop is set up in a way that cannot work. Raised at propose time, before a message
    goes out, because the alternative is a proposal nobody can approve sitting in a mailbox
    until it expires."""


class NoApproversError(ConfigurationError):
    """A proposal with no one able to say yes. Would wait forever."""


class PolicyError(ConfigurationError):
    """Internally inconsistent policy — nudges scheduled after expiry, a nudge cap larger than
    the schedule, a threshold below one."""


# --------------------------------------------------------------------------- runtime


class ProposalNotFound(DetachedError):
    pass


class UnknownApprover(DetachedError):
    """A decision was offered for someone who is not an approver on this proposal. Refused
    rather than ignored: silently dropping it makes a mis-addressed approval look like
    silence, and the loop then nudges a person who already answered."""


class AlreadyExecuted(DetachedError):
    """Execution was attempted on a proposal that has already run. Idempotency is the point —
    an hourly apply tick will see the same settled proposal repeatedly, and the second run
    must not double-write."""


class TransportError(DetachedError):
    """The transport could not send or read. Deliberately not swallowed: a propose tick whose
    send failed must not advance a cursor, or that window's signal is lost silently."""


class CursorNotCommittable(DetachedError):
    """`CursorRun.advance()` was called while a declared precondition was still unsatisfied.

    This is the structural version of a rule that is otherwise a comment nobody reads: the
    watermark may only move after both the durable write and the send have succeeded. A
    cursor that advanced without them skips that window forever, and skips it *silently*,
    which is the worst available failure mode.
    """
