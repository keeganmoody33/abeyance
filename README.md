# abeyance — disposable agents, durable work

Coordinate humans, machines, and models over days without a durable agent or a durable workflow
owning the work. The record is a row; the contributors are containers that appear, contribute,
and disappear.

Two layers, and you can use either alone:

- **[Approval](#most-approval-systems-make-the-agent-runtime-own-the-wait)** — durable,
  multi-party consent for cron, serverless, and batch agents. Consent detached from whatever
  asked for it.
- **[Cases](docs/CASES.md)** — the same detachment applied to work needing more than one kind of
  contributor. Typed contributions, policy-derived authority, one ephemeral container per piece
  of evidence.

> **abeyance**, *n.* — a state of temporary suspension; in law, a right that exists and is
> currently held by nobody, pending determination. That is the mechanism exactly: the
> authority to act is real, no process is holding it, and it resolves when the people with
> standing decide.

```bash
pip install abeyance        # core has zero dependencies
```

## Most approval systems make the agent runtime own the wait

That is often the right design. Durable runtimes already survive process death: LangGraph
persists an `interrupt()` through a checkpointer and resumes it later; Temporal holds
workflow state and takes a signal days afterwards. If your work already lives inside one of
those, use its approval primitive.

`abeyance` is for the case where you do not want the runtime that asked to own the wait —
or where there is no runtime at all, just a shell script on a cron entry.

**It separates consent from execution.** An agent proposes a bounded batch and exits
completely. The approval record, the approver identities, the partial decisions, the expiry,
the escalation and the receipt live independently of whatever produced them. Later — on
another host, from a different process, possibly a plain `cron` line with no agent in it —
a worker applies only the items that settled.

```
propose            send  ──▶  [ process exits completely ]
(any agent, or no agent)                │
                     the consent record persists on its own
                                        │
apply (any worker) ──▶  poll ──▶ record ──▶ execute only what settled ──▶ receipt
```

## Where it sits

| System | What it owns | Where `abeyance` fits |
|---|---|---|
| **LangGraph** / **Temporal** | Durable execution and resume-in-place | Consent detached from the agent runtime, so cron, a queue consumer, or another host can apply it later |
| **[JamJet](https://github.com/jamjet-labs/jamjet)** | Runtime policy, approvals, budgets, replay, durable agent execution | Use it *after* policy decides a human is genuinely required; `abeyance` owns the asynchronous consent process from there |
| **[AgentGate](https://github.com/agentkitai/agentgate)** | Policy routing an action to a dashboard / Slack / Discord / email approver | `abeyance` is not an approval UI or a policy router — it is the durable multi-item decision ledger and the apply loop behind one |
| **[HumanLayer ACP](https://github.com/humanlayer/agentcontrolplane)** | Distributed agent scheduling with approval-gated MCP calls | `abeyance` is a library you add to unattended workflows you already have, not a control plane you adopt |
| **[Cloudflare Agents](https://github.com/cloudflare/agents)** | Examples of persisted approval state and multi-approver flows | `abeyance` packages the hard semantics — deadlock, unreachable items, expiry, consumed replies, receipts — as a reusable state machine |

Complementary, not competing. The gap it fills is narrow and specific: **the consent process
itself, as a durable object with its own lifecycle.**

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

# --- whatever proposes. Runs, sends, exits. ---
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
# --- the apply worker. A cron entry. Knows nothing but the store. ---
if poll := loop.poll():                    # deterministic. no model. no tokens.
    for pid in poll.actionable:
        for inbound in loop.read(pid):     # parses; records NOTHING
            loop.record_from(pid, inbound) # refuses anything hedged; escalates instead
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

## What it actually gets right

### A reply parser is a suggestion, never authority

```python
interpret("approve 1 but can you reword the second line first", n_max=3)
# → Suggestion(approve=[1], mode="explicit", conditional=True, confident=False)
```

That reply is a request for another draft. Reading it as a yes ships text nobody agreed to.
`interpret()` handles `"1 and 3"`, `"all except 2"`, `"1-4, skip 3"`, `"none of these"` — and
marks conditionals, bare numbers, and lone affirmations **not confident**. `read()` returns
suggestions and records nothing; `record()` takes explicit item numbers. Something with
judgment sits between them, and `record_from(..., require_confident=True)` escalates rather
than guessing.

### Every item gets a real verdict

Not a boolean. Five outcomes, and the distinctions carry decisions:

| Verdict | Meaning | What you do about it |
|---|---|---|
| `APPROVED` | cleared the threshold | run it |
| `REJECTED` | someone decided against it | do not run it, do not re-ask |
| `DEADLOCKED` | people with equal standing disagreed | **write nothing**, escalate to a human |
| `UNREACHABLE` | nobody vetoed it; it can no longer reach the threshold | worth re-proposing |
| `WAITING` | not enough information yet | ask again later |

`DEADLOCKED` is the one most systems collapse. Taking the majority view, or the most recent
reply, resolves a genuine human disagreement by accident of implementation. Here the item is
not written, the proposal ends `DEADLOCKED`, and an escalation goes to an owner who is
deliberately *not* an approver — off the consent path, on the exception path.

### A partial answer does not strand the rest of the batch

A five-item digest comes back with "approve 1 and 3". Under unanimity the other three can
never pass — but "she answered and passed over them" is not the same fact as "she has not
read it yet", and a system that reports both as `waiting` hangs until expiry with real
decisions dying inside it. `UNREACHABLE` names the difference so the remainder can be
re-proposed the same day.

### Consent happens in the channels people already use, and can span days

Email is the reference transport because a reply comes back from a phone, on a plane, at
11pm, without a login. Approvers are identified by the sender of their reply, tracked
independently, and can answer days apart. Nudges follow a schedule and stop at a cap, because
an uncapped reminder is how a useful loop becomes a filter rule.

Expiry runs off the last *recorded activity*, so a conversation in progress does not time out
mid-sentence — see [the exact rule](#expiry-precisely) below, which is narrower than "any
reply resets it".

### Every approval ends with a receipt

```
acme — here is what changed, and why.

  • Add Directors of Curriculum & Instruction to the target personas
      file: personas/district-leader.md

Left alone — you disagreed on item 3. Nothing was written for it and a
human has been asked to break the tie.
```

Sent as a **fresh thread**, optionally to a wider audience than the approvers. This is the leg
most approval systems skip, and its absence is why people stop trusting one.

### No silently skipped work

For loops that scan a source and propose on what is new, the watermark is guarded. A cursor
that advances after a failed send skips that window **permanently and silently** — no error,
no retry, nothing in a log, because from the system's point of view it was handled.

```python
with gate.begin("acme") as run:
    run.require("ledger", "the append-only record of what we read")
    run.require("digest", "the approval request itself")

    write_ledger(findings);          run.satisfied("ledger", ref=sha)
    result = loop.propose(...);      run.satisfied("digest", ref=result.id)

    run.advance(marks={"last_event": ts})   # raises if either is outstanding
```

The rule becomes an exception you cannot miss instead of a comment somebody has to remember.
`DueGate` (which subjects are due, plus a floor sweep so a dead trigger cannot masquerade as a
quiet week) is optional and documented in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## What this does not claim

The value here is in being exact, so:

**Serialized apply, not exactly-once across distributed workers.** `record()` is idempotent
per approver and `execute()` refuses an already-executed proposal, but two apply workers
reading the same proposal before either executes can both pass the status check; store writes
are last-write-wins. **Run one apply worker at a time**, or make your executor idempotent.
Optimistic concurrency on the store is planned and not shipped.

**Sender attribution is not cryptographic identity.** An approver is identified by the `From`
address on their reply. That is an operational control, not authentication — no DKIM check, no
signature verification. Adequate for an internal mailbox; not adequate on its own where
spoofing is in your threat model. Put a verified channel in front of it if it is.

<a id="expiry-precisely"></a>
**Expiry restarts on recorded activity, not on any inbound.** The clock moves when a decision
is `record()`ed, a reply is `dismiss()`ed, you `ask()` a clarification, or the proposal is
executed. Merely *fetching* an inbound reply with `read()` or `poll()` does not move it. That
is deliberate — otherwise an out-of-office auto-reply keeps a dead proposal alive forever — but
it means an ambiguous reply arriving near the deadline needs a deliberate act to extend it.
`record_from()` raises an `AMBIGUOUS_REPLY` escalation in exactly that case, so it is surfaced
rather than silent.

**Not a policy engine, a scheduler, or an agent framework.** If you want "auto-approve
anything under $50", decide that before you propose. Bring your own cron. This library's job
starts once a human is genuinely required and ends when the receipt is sent.

## Adapters

The core is a state machine and a parser with **no dependencies**. Everything touching the
world sits behind one of four protocols.

| Seam | Ships with | Extra |
|---|---|---|
| **Store** | `MemoryStore`, `JSONFileStore`, `PostgresStore` | `[postgres]` |
| **Transport** | `MemoryTransport`, `GmailTransport`, `SMTPIMAPTransport`, `CallableTransport` | `[gmail]`, else stdlib |
| **Notifier** | `SlackNotifier`, `WebhookNotifier`, `Console`, `Recording`, `Null` | stdlib |
| **Clock** | `SystemClock`, `FrozenClock` | — |

**Use a shared store the moment a second host exists.** Per-host state is not merely
inconvenient, it is silently wrong: each host reads its own cursors, so a machine that stopped
running the loop keeps a frozen snapshot it cannot distinguish from "nothing happened".

**Already have a mail helper?** `CallableTransport` wraps it — the migration is a one-file
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

`poll()` is deterministic by construction — state read, transport read, ids returned. No model
call, no interpretation, no writes. An hourly tick over a hundred open proposals costs a
hundred API reads and zero tokens, which is why a shell runner should call `poll` first and
exit when it prints an empty `actionable` list.

`inject` drives the whole state machine with no mailbox and no humans — the multi-approver
paths are the ones worth rehearsing before real people are on the thread.

## Cases — when the work needs more than one kind of contributor

The approval layer detaches consent from the runtime that asked for it. The **case layer** applies
the same move to work needing a machine to gather facts, a model to form an opinion, and a person
to decide — each arriving from its own process, with nothing alive in between.

```python
from abeyance import Capability, CapabilityRegistry, CaseLoop, Need, when_payload
from abeyance.adapters import FlyMachinesRunner, PostgresStore

registry = CapabilityRegistry([
    Capability(name="bison-evidence", image="postgres:16-alpine",
               produces=("campaign-performance",), reach=("db-read",),
               app="workers-data", timeout_seconds=120),
])

cases = CaseLoop("launches", store=PostgresStore(DSN), registry=registry,
                 runner=FlyMachinesRunner(app="workers-data"), approval=approval_loop,
                 rules=[when_payload("deliverability-check", given="campaign-performance",
                                     key="gone_quiet", carry=("client",))])

case = cases.open(action="launch-campaign", subject_key="acme",
                  needs=[Need("campaign-performance", spec={"client": "Acme"})])

# ... an hourly cron line, on any host, in any process ...
for report in cases.tick(harvest_standing={"owner@acme.com": ("launch-campaign",)}):
    if report.actionable:
        cases.execute(report.case_id, executor=launch)
```

Three properties, and each is the reason for a file:

**Authority comes from the contribution's type and the actor's standing — never from the payload.**
A model emitting `{"verdict": "approved", "authorized": true}` has said something with exactly zero
authority, and the refusal is reported rather than silent. `EVIDENCE` and `RECOMMENDATION` can
never authorize; only a `DECISION` from an actor whose standing covers the action can.
([`standing.py`](abeyance/standing.py))

**New behaviour is free; new reach costs a human.** A case can invent any instruction and hand it
to a registered worker as a `spec`. What it cannot do is conjure a worker reaching somewhere no
declared capability reaches — that blocks the case and asks a person. Minting a capability can
itself be run as a case, so the system extends itself through its own approval loop, with no
moment where a model grants itself new reach. ([`capability.py`](abeyance/capability.py))

**A dispatch that vanishes is detected.** You ask for a container, the platform accepts, and it
never boots. Nothing throws, and the request looks exactly like work in progress. A lease plus an
attempt count is what turns that into a retry and then a loud failure.
([`dispatch.py`](abeyance/dispatch.py))

One container per contribution is the isolation model: the worker that reads the campaign database
is a different container, in a different app, with a different secret set, from the one that
writes to your CRM. Not a permission a misconfiguration can widen. The cost is a container boot —
this is for work measured in hours to days, not a 200ms tool call.

See [`docs/CASES.md`](docs/CASES.md) for the full picture, including what is deliberately *not*
provided (replay-based recovery, retry policies, exactly-once) and where those live instead.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the state machine, the verdict rules, the seams, the concurrency boundary
- [`docs/CASES.md`](docs/CASES.md) — the case layer: typed contributions, standing, reach, dispatch leases, scoped authority
- [`docs/SMOKE-RUN.md`](docs/SMOKE-RUN.md) — a real run against Fly machines, live Postgres and a Gmail thread, including the two bugs it found that the test suite did not
- [`docs/FAILURE-MODES.md`](docs/FAILURE-MODES.md) — twelve silent failures the design is shaped around, each pinned to its test

## Licence

GNU AGPL-3.0-or-later — see [LICENSE](LICENSE).

The network clause is deliberate and worth reading before you embed this: if you modify
`abeyance` and offer it to users over a network, those users are entitled to your modified
source. Using it unmodified inside your own service does not trigger that. Same licence as
the other runtime-control repos in this line of work — [agent-guard](https://github.com/kkrlstrm/agent-guard),
[agent-tenancy](https://github.com/kkrlstrm/agent-tenancy), [cc-logger](https://github.com/kkrlstrm/cc-logger),
[wroteonly](https://github.com/kkrlstrm/wroteonly) — because a control plane you cannot inspect
is not a control plane.
