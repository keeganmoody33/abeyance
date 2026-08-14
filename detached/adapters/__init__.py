"""Concrete adapters for the four seams in `detached.ports`.

Imports here are lazy on purpose: `PostgresStore` and `GmailTransport` need optional extras,
and importing this package must not fail on a machine that only uses the in-memory or JSON
implementations.
"""
from __future__ import annotations

from .notifiers import (CallableNotifier, ConsoleNotifier, NullNotifier, RecordingNotifier,
                        SlackNotifier, WebhookNotifier)
from .stores import JSONFileStore, MemoryStore, PostgresStore
from .transports import (CallableTransport, GmailTransport, MemoryTransport,
                         SMTPIMAPTransport, address_of, strip_quoted)

__all__ = [
    "MemoryStore", "JSONFileStore", "PostgresStore",
    "MemoryTransport", "GmailTransport", "SMTPIMAPTransport", "CallableTransport",
    "NullNotifier", "ConsoleNotifier", "RecordingNotifier", "CallableNotifier",
    "SlackNotifier", "WebhookNotifier",
    "strip_quoted", "address_of",
]
