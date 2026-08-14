# Architecture

## The one decision everything follows from

**Consent is separate from execution.**

Durable agent runtimes already survive process death — LangGraph resumes an `interrupt()` from
a checkpointer, Temporal takes a signal against durable workflow state. The distinction here is
not durability, it is *ownership*: the consent record is a first-class object with its own
lifecycle, not a decision a running workflow is holding open. Any worker that can reach the
store carries it forward, including one with no agent in it at all.

Every design choice below is downstream of that. A workflow holding the wait keeps continuity
in a live frame — it knows who it asked, what it has already read, and where it was. With
consent detached, none of that is implied, so a durable proposal, sender-attributed replies, a
consumed-reply set, an idempotent execute, a free polling gate, and a guarded watermark each
have to be made explicit. That is most of what this library *is*.

**Scope, before the detail:** run one apply worker at a time (see
[Concurrency](#concurrency)), and treat sender attribution as an operational control rather
than authentication.

```
┌─ propose tick ──────────────────────────────────────────────────────┐
│  DueGate.evaluate  →  gather  →  render  →  Transport.send          │
│                                              ↓                       │
│                                        Store.put(proposal)           │
│                                        CursorRun.advance()           │
└──────────────────────────────────────────────────────────────────────┘
                                   ⋮
                  no process · hours or days · possibly a redeploy
                                   ⋮
┌─ apply tick (hourly) ───────────────────────────────────────────────┐
│  poll()      Store.items + Transport.fetch      free, deterministic  │
│  read()      + interpret()                      a suggestion only    │
│  record()    per-approver ledger                explicit numbers     │
│  verdicts()  pure function of (proposal, policy)                     │
│  execute()   only APPROVED items → your executor                     │
│  receipt()   fresh thread, possibly wider audience                   │
└──────────────────────────────────────────────────────────────────────┘
```

## Modules

| Module | Owns | Depends on |
|---|---|---|
| `models.py` | `Item`, `Approver`, `Proposal`, `Status`, `Verdict`, outcomes | nothing |
| `policy.py` | thresholds, veto, expiry, turns, nudge schedule | nothing |
| `verdict.py` | **the approval math** — pure `(proposal, policy) → verdicts` | models, policy |
| `interpret.py` | free text → `Suggestion` | nothing |
| `cursor.py` | `DueGate`, `Cursor`, `CursorRun` (the watermark guard) | ports |
| `loop.py` | the orchestration and every state transition | all of the above |
| `render.py` | the words a human reads | models |
| `ports.py` | the four Protocols + clocks | models |
| `adapters/` | concrete stores, transports, notifiers | optional extras |

`verdict.py` is deliberately pure and dependency-free: "who was allowed to write this" is the
question asked after an incident, and it should be answerable by reading one screen, and
testable without standing anything up.

## Status lifecycle

```
                    ┌──────────────────┐
     propose ──────▶│  AWAITING_REPLY  │
                    └────────┬─────────┘
                             │ some approvers answered
                    ┌────────▼──────────────┐
                    │  PARTIALLY_APPROVED   │◀────┐
                    └────────┬──────────────┘     │ record()
                             │                     │
              ask() ─────────┼──────────────▶ CLARIFYING
                             │                     │  turns > max_turns
   all settled, execute()    │                     ▼
                    ┌────────▼─────┐          ┌─────────┐
                    │   EXECUTED   │          │ STALLED │
                    └──────────────┘          └─────────┘
                             │ any item deadlocked
                    ┌────────▼─────┐          ┌─────────┐
                    │  DEADLOCKED  │          │ EXPIRED │◀── sweep(), last_activity
                    └──────────────┘          └─────────┘
```

`EXECUTED`, `DEADLOCKED`, `STALLED` and `EXPIRED` are terminal. `execute()` on an already-
executed proposal raises `AlreadyExecuted` — an hourly apply tick will see the same settled
proposal repeatedly and must not double-write.

## The verdict rules, in order

For each item, given `yes` / `no` / `silent` sets over the approvers:

1. **Advisory item** → `APPROVED` (nothing to gate; never reaches an executor).
2. **No approvers** → `WAITING`. Vacuous truth would otherwise approve everything.
3. **`veto` and any `no`** → `DEADLOCKED` if anyone also said yes, else `REJECTED`.
4. **`len(yes) >= threshold`** → `APPROVED`.
5. **Threshold no longer reachable** → `REJECTED` if anyone objected, else `UNREACHABLE`.
6. Otherwise → `WAITING`.

Step 5 depends on `policy.silence_after_reply`:

- `"abstain"` (default) — an approver's reply *was* their answer, so items they passed over
  cannot reach the threshold. This is what makes an unattended loop terminate.
- `"waiting"` — they may still speak. Correct only with a human driving follow-ups; on a cron
  it means waiting forever.

`ask()` reopens the question for whoever it addresses (clears `replied_at`, keeps the ledger),
so a clarification round is never blocked by this setting.

## The four seams

Protocols, not base classes — an adapter is anything with the right methods, including the
mail helper you already have.

**Store** — `get / put / items / delete` over `(kind, key) → dict`. `items()` must be one
round trip: the poll tick calls it every run, and an N+1 there is the difference between a
cheap gate and an expensive one.

**Transport** — `address`, `send`, `fetch_replies`. Two hard requirements: every reply carries
its **sender**, and our own messages are excluded **by sender as well as by id**.

**Notifier** — `notify(channel_id, message)`. Separate from Transport because a nudge should
arrive somewhere other than the mailbox already being ignored. The cap lives in the policy, so
an adapter cannot opt out of it.

**Clock** — `now() → int`. Injectable so a seven-day expiry and a 72-hour second nudge are
testable in microseconds. Untestable time-based behaviour is untested time-based behaviour.

## Where the model goes

Nowhere inside the library, on purpose. The natural division on a run that uses one:

| Step | Who | Why |
|---|---|---|
| decide what to propose | your agent | it is the judgment you are automating |
| render the digest | `Renderer` | deterministic, and you want it diffable |
| `poll()` | library | **must stay free of model calls** — the whole cost argument |
| read a reply | `interpret()` first, model on anything not `confident` | most replies are `"approve 1,3"` |
| decide what it meant | model or human | never a regex |
| `record()` / `execute()` | library | deterministic given the decision |

<a id="concurrency"></a>
## Concurrency — safe for a serialized apply worker

**This library does not provide exactly-once execution across distributed workers, and should
not be described as if it does.** It is safe for a serialized apply worker. Two apply ticks
racing on one proposal (an overlapping cron, or a manual run beside the scheduled one) is the
realistic failure. The current guarantees:

- `record()` is idempotent per approver — it replaces a ledger rather than appending, so the
  same reply processed twice reaches the same state.
- `execute()` refuses a proposal already `EXECUTED`.
- `Store.put` is last-write-wins.

That leaves one genuinely unhandled window: two ticks reading the same proposal *before*
either has executed can both pass the status check. If your executor is not itself idempotent,
serialise the apply tick — a lock file, `SELECT … FOR UPDATE`, or simply not running two.
Optimistic concurrency on the store is a planned addition; it is called out here rather than
implied to be solved.

## Trust boundaries

**Sender attribution is not authentication.** An approver is identified by the `From` address
on their reply, lowercased and matched against the approver set. There is no DKIM validation,
no signature check, no proof the human named actually typed it. That is an *operational*
control — adequate inside a mailbox you administer, inadequate on its own anywhere spoofing is
in the threat model. If it is, put a verified channel in front of the transport, or use a
`CallableTransport` wrapping one that authenticates.

What the library does do at this boundary: refuse to count a decision from an address that is
not an approver (`UnknownApprover`), and raise an `UNKNOWN_SENDER` escalation when a stranger
replies to a thread rather than dropping it silently — a forwarded proposal answered by the
wrong person is exactly the case you want to hear about.

**The parser is not an authority.** `interpret()` is a convenience for the unambiguous
majority of replies. It never records. Conditional, bare-number, affirmation-only and
unparseable replies come back `confident=False`, and `record_from(require_confident=True)`
escalates rather than guessing. Consent is what a person decided, not what a regex extracted.

**Expiry moves on recorded activity only** — `record`, `dismiss`, `ask`, `confirm`, `execute`.
Fetching an inbound with `read()` or `poll()` does not move it. See
[`docs/FAILURE-MODES.md`](FAILURE-MODES.md) §3 for why, and what it costs.

## Testing

124 tests, all on in-memory adapters, no network and no credentials. The ones worth reading
first, because each pins a specific silent failure:

| Test | Failure it prevents |
|---|---|
| `test_transport_safety.py::test_our_own_message_is_never_read_as_a_reply` | reading our own proposal as consent |
| `test_transport_safety.py::test_a_recorded_reply_does_not_resurface_forever` | a loop permanently "actionable" |
| `test_lifecycle.py::test_replying_restarts_the_expiry_clock` | a live negotiation dying at the deadline |
| `test_verdict.py::test_split_is_a_deadlock_not_a_majority` | a disagreement laundered into a write |
| `test_cursor.py::test_advance_refuses_while_a_precondition_is_outstanding` | a window skipped forever, silently |
| `test_loop.py::test_execute_is_idempotent` | an hourly tick double-writing |
