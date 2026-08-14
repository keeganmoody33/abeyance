# Changelog

## Unreleased

**Positioning corrected.** The original framing claimed every human-in-the-loop library
requires a live process holding the wait. That is false — LangGraph resumes an `interrupt()`
from a checkpointer, Temporal signals durable workflow state, both surviving process death.
The real category is **durable consent detached from the agent runtime**, which makes this
complementary to those systems rather than a competitor to them. README, `__init__`, `loop`,
the architecture doc and the two-approver example rewritten accordingly, plus a table of where
it sits against LangGraph/Temporal, JamJet, AgentGate, HumanLayer ACP and Cloudflare Agents.

**Limits documented rather than implied.** An explicit "what this does not claim": safe for a
*serialized* apply worker and not exactly-once across distributed workers; sender attribution
is an operational control, not authentication; and the exact expiry rule.

**Expiry rule stated precisely and pinned by tests.** `last_activity_epoch` moves on *recorded*
activity — `record`, `dismiss`, `ask`, `confirm`, `execute` — and not on merely fetching an
inbound with `read()`/`poll()`. Deliberate, since resetting on any inbound would let an
out-of-office keep a dead proposal alive forever. Two new tests cover both halves, including
the cost: an ambiguous reply near the deadline needs a deliberate act, which is why
`record_from()` raises `AMBIGUOUS_REPLY` instead of running out the clock.

## 0.1.0 — unreleased

First cut. Extracted from eleven near-identical propose/apply loops running in production
against ~35 client workspaces, plus a two-track, dual-approval loop that pushed the shape past
what a single-approver implementation could carry.

**Core**
- `ApprovalLoop` — propose, poll, read, record, ask, execute, confirm, receipt, nudge, sweep.
- Abeyance lifecycle: the proposing process exits; any host with the store resumes.
- `poll()` is deterministic and free of model calls by construction.

**Consent**
- `ApprovalPolicy`: threshold (`ALL` / N), veto, expiry, turn cap, nudge schedule,
  `silence_after_reply`, `roles_required`, `allow_self_approval`.
- Five verdicts — `APPROVED`, `REJECTED`, `DEADLOCKED`, `UNREACHABLE`, `WAITING`.
- Per-approver ledgers keyed on the reply's sender; `DEADLOCKED` writes nothing and escalates.

**Replies**
- `interpret()` → `Suggestion`, never a decision. Ranges, `all except`, blanket rejections,
  custom vocabularies; conditionals and lone affirmations marked not-confident.
- Consumed-reply tracking, and `dismiss()` for replies that are not decisions.

**Scanning loops**
- `DueGate` with cheap/expensive triggers, blocking preconditions, and a floor sweep.
- `CursorRun` — the watermark advances only when every declared precondition is satisfied.

**Adapters**
- Stores: memory, JSON file (atomic writes), Postgres.
- Transports: memory, Gmail, SMTP+IMAP, callable wrapper.
- Notifiers: Slack, webhook, console, recording, null.
- `FrozenClock` for time-dependent paths.

**Surfaces**
- `abeyance` CLI with categorical exit codes and a `inject` rehearsal command.
- `PlainTextRenderer` for digests, receipts, and escalation summaries.

124 tests, no network, no credentials. Core install has zero dependencies.
