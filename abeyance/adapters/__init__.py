"""Concrete adapters for the five seams in `abeyance.ports`.

Imports here are lazy on purpose: `PostgresStore` and `GmailTransport` need optional extras,
and importing this package must not fail on a machine that only uses the in-memory or JSON
implementations. `FlyMachinesRunner` needs no extra at all — it speaks the Machines API over
stdlib HTTP, because a runner that dragged in an SDK would put a dependency in the path of
every worker dispatch.
"""
from __future__ import annotations

from .notifiers import (CallableNotifier, ConsoleNotifier, NullNotifier, RecordingNotifier,
                        SlackNotifier, WebhookNotifier)
from .runners import FlyMachinesRunner, LocalProcessRunner, MemoryRunner
from .stores import JSONFileStore, MemoryStore, PostgresStore
from .transports import (CallableTransport, GmailTransport, MemoryTransport,
                         SMTPIMAPTransport, address_of, strip_quoted)

__all__ = [
    "MemoryStore", "JSONFileStore", "PostgresStore",
    "MemoryTransport", "GmailTransport", "SMTPIMAPTransport", "CallableTransport",
    "NullNotifier", "ConsoleNotifier", "RecordingNotifier", "CallableNotifier",
    "SlackNotifier", "WebhookNotifier",
    "MemoryRunner", "LocalProcessRunner", "FlyMachinesRunner",
    "strip_quoted", "address_of",
]
