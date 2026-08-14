# Changelog

## 0.1.0 — unreleased

First cut. Extracted from eleven near-identical propose/apply loops running in production
against ~35 client workspaces, plus a two-track, dual-approval loop that pushed the shape past
what a single-approver implementation could carry.

**Core**
- `ApprovalLoop` — propose, poll, read, record, ask, execute, confirm, receipt, nudge, sweep.
- Detached lifecycle: the proposing process exits; any host with the store resumes.
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
- `detached` CLI with categorical exit codes and a `inject` rehearsal command.
- `PlainTextRenderer` for digests, receipts, and escalation summaries.

124 tests, no network, no credentials. Core install has zero dependencies.
