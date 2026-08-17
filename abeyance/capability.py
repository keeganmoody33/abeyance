"""What workers exist, and what each one is allowed to reach.

This is the ceiling, and the ceiling is the product.

Every agent framework answers "what can this agent do?" with a tool list the model picks from
at runtime. This answers it with a declared set of container images, each with an explicit
`reach` — the credentials and network it can touch. The consequence is the property the whole
design turns on:

    **New behaviour is free. New reach costs a human.**

A case can invent any instruction it likes and hand it to a registered worker as a `spec` —
that is Tier 1, instant, ungated, and it covers almost everything. What a case cannot do is
conjure a worker that reaches somewhere no registered capability reaches. When the evaluator
needs something nothing can produce, `require()` raises and the case blocks with a named
escalation. It does not improvise, and it does not silently proceed with the evidence it
happens to have.

Minting a new capability is therefore a deliberate act: write the image, declare its reach,
get it reviewed. That path can itself be run as a case (a build worker drafts it, a human with
standing decides), which is how the system extends itself without any moment where a model
grants itself new reach. If you let a model generate a Dockerfile and auto-build-and-run it
with no human in between, you have built remote code execution with extra steps — the gate is
not friction, it is the entire security model.

    registry = CapabilityRegistry([
        Capability(name="bison-evidence", image="postgres:16-alpine",
                   produces=("campaign-performance",), emits=ContributionKind.EVIDENCE,
                   reach=("neon-read",), app="abeyance-workers"),
    ])
    registry.require("campaign-performance")     # -> the Capability, or raises
    registry.require("wire-money")               # -> CapabilityMissing
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .errors import CapabilityMissing, ConfigurationError
from .models import ContributionKind


@dataclass(frozen=True)
class Capability:
    """One declared worker: an image, what it can produce, and what it can reach.

    `reach` is the field that matters and the field this library never verifies. These are
    labels describing the credentials and network the *image* is built and deployed with —
    abeyance cannot enforce them, and pretending otherwise would be worse than not having
    them. What they buy is reviewability: the registry is a small file, so "which workers can
    touch production?" is a diff and a grep rather than an audit project. Enforcement lives
    where it belongs, in what secrets the container is actually given.
    """

    name: str
    image: str
    """A fully-qualified image reference. Pin a digest for anything that matters — a mutable
    tag means the reviewed capability and the running capability can differ silently."""

    produces: Sequence[str] = ()
    """Need-labels this capability can satisfy. The evaluator matches on these."""

    emits: ContributionKind = ContributionKind.EVIDENCE
    """The kind of contribution this worker produces. A worker may never emit DECISION and
    `__post_init__` refuses to declare one — authority is not a capability you can deploy."""

    reach: Sequence[str] = ()
    """Credential/network labels this image is deployed with: "neon-read", "clickup-write",
    "public-internet". Documentation for humans and the input to a review, not a sandbox."""

    app: str = ""
    """The platform app that owns this worker's secrets. On Fly this is the app the machine is
    created in, and it is the real isolation boundary: secrets live on the app, so a worker
    physically cannot read another capability's credentials. Prefer one app per reach profile
    over one shared app with every secret on it."""

    entrypoint: Sequence[str] = ()
    """Overrides the image's own entrypoint. Needed whenever a stock image is reused as a
    worker host: `postgres:16-alpine` would otherwise try to start a database server, and the
    args you meant as a script would arrive as arguments to that."""

    cmd: Sequence[str] = ()
    """Command (or entrypoint args). The spec is passed separately, as env, so the command line
    stays identical across invocations and the image can be reviewed independently of any one
    job — which is what makes "review the image once, vary the instruction freely" work."""

    env: Dict[str, str] = field(default_factory=dict)
    """Static env for every invocation. Never put a secret here — it would sit in the registry
    and therefore in git. Secrets belong on the app named above."""

    guest: Dict[str, Any] = field(default_factory=dict)
    """Runner-specific sizing, e.g. {"cpus": 1, "memory_mb": 512}."""

    timeout_seconds: int = 600
    """How long this worker is expected to take. The dispatcher's lease is derived from it, so
    a worker that habitually overruns its declared timeout gets re-dispatched — set it
    honestly."""

    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ConfigurationError("a capability needs a name")
        if not self.image:
            raise ConfigurationError(f"capability {self.name!r} has no image")
        if self.emits is ContributionKind.DECISION:
            raise ConfigurationError(
                f"capability {self.name!r} declares emits=DECISION — a worker cannot be given "
                "authority by declaring it. Decisions come from actors with standing "
                "(see standing.py); workers emit EVIDENCE or RECOMMENDATION.")
        if not self.produces:
            raise ConfigurationError(
                f"capability {self.name!r} produces nothing — the evaluator matches needs "
                "against `produces`, so it could never be selected")

    def can_produce(self, need: str) -> bool:
        return need in self.produces

    def to_doc(self) -> Dict[str, Any]:
        return {"name": self.name, "image": self.image, "produces": list(self.produces),
                "emits": self.emits.value, "reach": list(self.reach), "app": self.app,
                "entrypoint": list(self.entrypoint), "cmd": list(self.cmd),
                "env": dict(self.env), "guest": dict(self.guest),
                "timeout_seconds": self.timeout_seconds, "description": self.description}

    @classmethod
    def from_doc(cls, d: Dict[str, Any]) -> "Capability":
        return cls(name=d["name"], image=d["image"],
                   produces=tuple(d.get("produces") or ()),
                   emits=ContributionKind(d.get("emits", ContributionKind.EVIDENCE.value)),
                   reach=tuple(d.get("reach") or ()), app=d.get("app", ""),
                   entrypoint=tuple(d.get("entrypoint") or ()),
                   cmd=tuple(d.get("cmd") or ()), env=dict(d.get("env") or {}),
                   guest=dict(d.get("guest") or {}),
                   timeout_seconds=int(d.get("timeout_seconds") or 600),
                   description=d.get("description", ""))


class CapabilityRegistry:
    """The set of workers that exist. Small, explicit, and reviewable in a diff.

    Deliberately not discovered by scanning a directory or querying the platform for whatever
    images happen to be deployed. A registry you can enumerate by accident is a registry
    nobody reviews, and the review is the only thing standing between "the model wrote a new
    instruction" and "the model acquired new reach".
    """

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._by_name: Dict[str, Capability] = {}
        for c in capabilities:
            self.add(c)

    # ----------------------------------------------------------------- building

    def add(self, cap: Capability) -> "CapabilityRegistry":
        if cap.name in self._by_name:
            raise ConfigurationError(f"capability {cap.name!r} is already registered")
        self._by_name[cap.name] = cap
        return self

    @classmethod
    def from_docs(cls, docs: Iterable[Dict[str, Any]]) -> "CapabilityRegistry":
        return cls(Capability.from_doc(d) for d in docs)

    @classmethod
    def from_file(cls, path: "str | Path") -> "CapabilityRegistry":
        """Load from a JSON file — the reviewable form. A list, or {"capabilities": [...]}."""
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        docs = raw.get("capabilities", []) if isinstance(raw, dict) else raw
        return cls.from_docs(docs)

    # ----------------------------------------------------------------- lookup

    def get(self, name: str) -> Optional[Capability]:
        return self._by_name.get(name)

    def names(self) -> List[str]:
        return sorted(self._by_name)

    def all(self) -> List[Capability]:
        return [self._by_name[n] for n in self.names()]

    def match(self, need: str) -> Optional[Capability]:
        """The capability that can produce `need`, or None. Never invents one.

        Ambiguity is a configuration error rather than a silent pick: if two capabilities both
        claim a need, the choice would be arbitrary and the arbitrary choice would be the one
        holding credentials.
        """
        found = [c for c in self.all() if c.can_produce(need)]
        if len(found) > 1:
            raise ConfigurationError(
                f"need {need!r} is claimed by {[c.name for c in found]} — exactly one "
                "capability may produce a need, or dispatch would pick one arbitrarily")
        return found[0] if found else None

    def require(self, need: str) -> Capability:
        """`match`, but raising. This is the honest failure at the ceiling.

        A case that needs something no worker can produce has reached the edge of what has
        been reviewed. It stops and asks a human. It does not approximate the need with a
        worker that reaches somewhere else, and it does not proceed without it.
        """
        cap = self.match(need)
        if cap is None:
            raise CapabilityMissing(
                f"no registered capability produces {need!r}. Reachable needs: "
                f"{sorted({n for c in self.all() for n in c.produces})}. Minting a new "
                "capability is a human decision — declare an image and its reach in the "
                "registry; the case blocks until then.")
        return cap

    # ----------------------------------------------------------------- audit

    def reach_report(self) -> Dict[str, List[str]]:
        """`{reach_label: [capability names]}` — "what can touch production?" in one call."""
        out: Dict[str, List[str]] = {}
        for c in self.all():
            for r in c.reach:
                out.setdefault(r, []).append(c.name)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def to_doc(self) -> Dict[str, Any]:
        return {"capabilities": [c.to_doc() for c in self.all()]}

    def __len__(self) -> int:
        return len(self._by_name)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CapabilityRegistry {self.names()}>"
