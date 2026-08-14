# abeyance — human approval for agents that aren't running

## Every human-in-the-loop library assumes something is alive, waiting. Unattended agents aren't.

The standard model of agent approval is an **interrupt**. An agent is mid-run, it reaches a
tool call marked "needs approval", it pauses, a human answers, it resumes. LangGraph's
`interrupt()`, the OpenAI Agents SDK's `needs_approval`, HumanLayer, Cloudflare Agents — all
the same shape, and it is the right shape for an interactive session.

It does not survive contact with unattended work. A cron job at 07:30 cannot hold a process
open until Thursday. The machine that asked will be reclaimed. The approver is asleep, then in
meetings, then on a plane, and the honest median time-to-answer is *a day and a half*, not
ninety seconds.

So teams do the thing you have seen: the agent asks, and then **acts anyway**. Or it does not
ask at all. The approval gate becomes a log line.

`abeyance` inverts it. **The wait is not a paused process. It is a row in a store.**

> **abeyance**, *n.* — a state of temporary suspension; in law, a right that exists and is
> currently held by nobody, pending determination. That is the mechanism exactly: the
> authority to act is real, no process is holding it, and it resolves when the people with
> standing decide.

```
propose tick  ──▶  send  ──▶  [ PROCESS EXITS ]
                                     │
                  hours or days — no session, no held connection, no running agent
                                     │
apply tick    ──▶  cheap gate ──▶ replies? ──▶ record ──▶ execute ──▶ receipt
```

The propose tick renders a batch of numbered items, sends one digest, writes state, and
terminates. Some hours later a different tick — possibly on a different host, definitely not
the same process — notices a reply, works out who approved what, and executes exactly what
cleared. Nothing was running in between.

```bash
pip install abeyance        # core has zero dependencies
```

## What that buys you

| | Interrupt model | `abeyance` |
|---|---|---|
| During the wait | a process is alive holding it | nothing is running |
| Time horizon | a session | days, across restarts and redeploys |
| Who can resume | the process that asked | any host with the store |
| Approvers | one waiter | **N, independently, at different times** |
| Disagreement | last writer wins / undefined | **`deadlocked` — no write, escalate** |
| Cost of waiting | tokens or a held connection | one API read per open proposal |
| Unit of consent | one tool call | a ranked batch, answered in prose |

## The 60-second version

```python
from abeyance import ApprovalLoop, Item, Approver, ApprovalPolicy
from abeyance.adapters import PostgresStore, GmailTransport

loop = ApprovalLoop(
    "migrations",
    store=PostgresStore(os.environ["DATABASE_URL"]),      # shared, not per-host
    transport=GmailTransport(token_path="~/.gmail/token.json"),
    policy=ApprovalPolicy(threshold="all", veto=True,     # both must say yes
                          expire_after_days=7, max_turns=3),
)

# --- the propose tick. Runs, sends, exits. ---
loop.propose(
    items=[Item(n=1, summary="Drop legacy_sessions (0 reads in 90d)",
                payload={"table": "legacy_sessions"}),
           Item(n=2, summary="Backfill tenant_id on 4.1M rows",
                payload={"job": "backfill-tenant"})],
    approvers=[Approver("dba@corp.com", role="dba", channel_id="U123"),
               Approver("lead@corp.com", role="lead", channel_id="U456")],
    subject_key="prod-migration-114",
)
```

Someone replies, in prose, from their phone:

> approve 1, hold 2 until after the release

```python
# --- the apply tick. A cron entry. Knows nothing but the store. ---
if poll := loop.poll():                    # deterministic. no model. no tokens.
    for pid in poll.actionable:
        for inbound in loop.read(pid):     # parses; records NOTHING
            loop.record_from(pid, inbound)  # refuses anything hedged; escalates instead
        loop.execute(pid, executor=run_migration)
```

`run_migration` is called for item 1 and never for item 2 — and never at all until **both**
approvers have answered.

Run it now, no credentials needed:

```bash
git clone https://github.com/kkrlstrm/abeyance && cd abeyance
pip install -e ".[dev]" && python -m pytest -q

python examples/01_single_approver.py              # the whole library in 40 lines
python examples/02_two_approvers_and_a_deadlock.py # two people, one disagreement
python examples/03_scheduled_worker.py             # the production cron shape
```

## The five things this gets right that a bolted-on approval does not

### 1. Disagreement is not resolved by the machine

Two people with standing to decide said opposite things about the same change. Taking the
majority view, or the most recent, launders a real disagreement into a write.

```python
verdicts == {1: APPROVED, 2: APPROVED, 3: DEADLOCKED}
```

A deadlocked item **is not written**, the proposal ends `DEADLOCKED`, and an escalation goes to
an owner who is deliberately *not* an approver — off the consent path, on the exception path.

There are five verdicts, and the two extra ones carry real information:

- `REJECTED` — a decision was made against it. Do not re-ask.
- `UNREACHABLE` — nobody vetoed it; it just cannot reach the threshold now. **Worth
  re-proposing.** Without this, "she answered and passed over items 2–5" is indistinguishable
  from "she hasn't read it yet", and the batch hangs until it expires with real decisions
  inside it.

### 2. The gate is free

`poll()` is deterministic by construction — it reads state, reads the transport, returns ids.
No model call, no interpretation, no writes. An hourly tick across a hundred open proposals
costs a hundred API reads and zero tokens, which is the entire reason this can run for months
instead of being switched off after the first invoice.

### 3. Parsing a reply is a suggestion, never a decision

```python
interpret("approve 1 but can you reword the second line first", n_max=3)
# → Suggestion(approve=[1], mode="explicit", conditional=True, confident=False)
```

That reply is a request for another draft. Reading it as a yes ships text nobody agreed to.
`interpret()` handles `"1 and 3"`, `"all except 2"`, `"1-4, skip 3"`, `"none of these"` — and
flags conditionals, bare numbers, and lone affirmations as **not confident**. `read()` returns
suggestions and records nothing; `record()` takes explicit numbers. Something with judgment
sits between them.

### 4. Expiry runs off the last activity, not the send

Anchor expiry on send time and a live negotiation dies at the deadline, mid-sentence, while
both parties are actively talking. Replying restarts the clock. Silence still ends it — and
loudly: every expiry raises an escalation, because an expiry that passes quietly is
indistinguishable from a healthy week and the work inside it dies unnoticed.

Turns are capped too (default 3). A loop that keeps asking gets filtered, and then the *next*
real proposal is ignored as well.

### 5. The people who said yes are told what it did

```
acme — here is what changed, and why.

  • Add Directors of Curriculum & Instruction to the target personas
      file: personas/district-leader.md

Left alone — you disagreed on item 3. Nothing was written for it and a
human has been asked to break the tie.
```

Sent as a **fresh thread**, optionally to a wider audience than the approvers. This is the leg
almost every approval system skips, and its absence is why people stop trusting one.

## The watermark guard

For loops that scan a source and propose on what is new, `DueGate` answers "is this subject
due?" and `CursorRun` answers the more dangerous question: *may the watermark move yet?*

```python
with gate.begin("acme") as run:
    run.require("ledger", "the append-only record of what we read")
    run.require("digest", "the approval request itself")

    write_ledger(findings);          run.satisfied("ledger", ref=sha)
    result = loop.propose(...);      run.satisfied("digest", ref=result.id)

    run.advance(marks={"last_event": ts})   # raises if either is outstanding
```

A cursor that advances after a failed send skips that window **permanently and silently** —
no error, no retry, no gap anywhere to notice. This makes it an exception instead of a comment
somebody has to remember.

The floor sweep (`floor_days`, default 14) is not optional either. Trigger-only scheduling
means a subject whose source breaks — revoked token, renamed channel, an integration quietly
returning `[]` — is never due again and looks exactly like a healthy quiet subject.

## Adapters

The core is a state machine and a parser with **no dependencies**. Everything that touches the
world is behind one of four protocols, so bring your own.

| Seam | Ships with | Extra |
|---|---|---|
| **Store** | `MemoryStore`, `JSONFileStore`, `PostgresStore` | `[postgres]` |
| **Transport** | `MemoryTransport`, `GmailTransport`, `SMTPIMAPTransport`, `CallableTransport` | `[gmail]`, else stdlib |
| **Notifier** | `SlackNotifier`, `WebhookNotifier`, `Console`, `Recording`, `Null` | stdlib |
| **Clock** | `SystemClock`, `FrozenClock` | — |

Two notes that will save you an afternoon:

**Use a shared store the moment a second host exists.** Per-host state is not merely
inconvenient, it is silently wrong: each host reads its own cursors, so a machine that stopped
running the loop keeps a frozen snapshot it cannot distinguish from "nothing happened". The
symptom is a digest confidently reporting work as outstanding that was applied days ago.

**Already have a mail helper?** `CallableTransport` wraps it. The migration is a one-file
change, not a credentials project.

## CLI

Every subcommand prints JSON; exit codes are categorical so a shell runner can branch without
parsing prose (`0` fine · `2` usage · `3` not found · `4` blocked · `5` transport).

```bash
abeyance --app myapp:loop poll                 # the free gate — exit early when quiet
abeyance --app myapp:loop read   --id T
abeyance --app myapp:loop record --id T --from a@b.com --approve 1,3
abeyance --app myapp:loop apply  --id T --executor myapp:run
abeyance --app myapp:loop nudge --dry-run
abeyance --app myapp:loop inject --id T --from a@b.com --text "approve 1"   # rehearsal
```

`inject` drives the whole state machine with no mailbox and no humans — the multi-approver
paths are the ones worth rehearsing before real people are on the thread.

## What this is not

- **Not a scheduler.** Bring cron, launchd, supercronic, Airflow, whatever you have.
- **Not an agent framework.** It holds no opinion about what produced the items.
- **Not a policy engine.** If you want "auto-approve anything under $50", decide that before
  you propose. This library's job starts once a human is genuinely required.
- **Not the right tool for a synchronous decision.** If someone is sitting there waiting for
  the answer, use an interrupt — that is what interrupts are for.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the state machine, the verdict rules, the seams
- [`docs/FAILURE-MODES.md`](docs/FAILURE-MODES.md) — every silent failure this is shaped around, and the test that pins it

## Licence

Apache-2.0.
