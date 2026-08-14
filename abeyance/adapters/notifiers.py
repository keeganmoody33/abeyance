"""Poking someone who has gone quiet, somewhere other than the mailbox they are ignoring.

A nudge is the only outbound message in the system that is not part of the record, which is
why it gets its own seam. It is also the easiest thing in the library to make actively
harmful: an uncapped reminder is how a useful loop becomes a filter rule. The cap lives in the
policy, not here — an adapter should not be able to opt out of it.

Slack is stdlib-only (`urllib`), so the core install still has zero dependencies.
"""
from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("abeyance.notify")


class NullNotifier:
    """Accepts and drops. The honest default when no channel is configured — a loop with no
    nudge path should still run, it just will not chase anyone."""

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        log.debug("nudge suppressed (NullNotifier) to %s: %s", channel_id or address, message)


@dataclass
class ConsoleNotifier:
    """Prints. For local runs and for seeing what a nudge schedule actually does before
    pointing it at real people."""

    stream: Any = field(default=None)

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        out = self.stream or sys.stderr
        print(f"[nudge -> {channel_id or address}] {message}", file=out)


@dataclass
class RecordingNotifier:
    """Records instead of sending. The test double, and useful in a dry-run pass."""

    sent: List[Dict[str, Any]] = field(default_factory=list)

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        self.sent.append({"channel_id": channel_id, "address": address, "message": message,
                          "context": dict(context or {})})


@dataclass
class CallableNotifier:
    """Wrap any function. `fn(channel_id, message, address=..., context=...)`."""

    fn: Callable[..., None]

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        self.fn(channel_id, message, address=address, context=context or {})


class SlackNotifier:
    """Direct message via the Slack Web API. Stdlib `urllib`; no SDK.

    `channel_id` is a Slack user id (`U…`) or an already-open conversation id. Bot tokens need
    `chat:write` and, for DMs to a user id, `im:write`. A token that can read a channel very
    often cannot DM anybody — verify with a message to yourself before wiring real approvers,
    because a nudge path that silently fails leaves you believing people are being chased when
    nobody is.
    """

    API = "https://slack.com/api/chat.postMessage"

    def __init__(self, token: str, *, timeout: int = 15, unfurl: bool = False) -> None:
        if not token:
            raise ValueError("SlackNotifier needs a bot token")
        self.token = token
        self.timeout = timeout
        self.unfurl = unfurl

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        if not channel_id:
            raise ValueError(f"no slack channel/user id for {address or 'approver'}")
        payload = json.dumps({"channel": channel_id, "text": message,
                              "unfurl_links": self.unfurl,
                              "unfurl_media": self.unfurl}).encode()
        req = urllib.request.Request(
            self.API, data=payload,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:  # pragma: no cover - network
            raise RuntimeError(f"slack unreachable: {e}") from e
        if not body.get("ok"):
            # Surface Slack's own error verbatim. `not_in_channel`, `missing_scope` and
            # `channel_not_found` each need a different fix and a generic message hides which.
            raise RuntimeError(f"slack refused the nudge: {body.get('error', 'unknown')}")


@dataclass
class WebhookNotifier:
    """POST a JSON body to a URL. For Discord, Teams, PagerDuty, or your own relay."""

    url: str
    timeout: int = 15
    template: Callable[[str, str, str, Dict[str, Any]], Dict[str, Any]] = field(
        default=lambda channel_id, message, address, context: {
            "channel": channel_id, "text": message, "address": address, "context": context})

    def notify(self, channel_id: str, message: str, *, address: str = "",
               context: Optional[Dict[str, Any]] = None) -> None:
        payload = json.dumps(self.template(channel_id, message, address, dict(context or {})))
        req = urllib.request.Request(
            self.url, data=payload.encode(),
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status >= 300:  # pragma: no cover - network
                raise RuntimeError(f"webhook returned {resp.status}")
