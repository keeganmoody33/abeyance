"""Where a worker actually runs.

Three runners, and the choice between them is the same kind of real decision as the choice of
store:

  `MemoryRunner`   tests. Records what would have started; you drive the state by hand, which
                   is the only way the lease, retry and give-up paths get covered.
  `LocalProcessRunner`  a subprocess on this machine. Real execution, no cloud, no image — the
                   honest way to develop a worker and to run a case layer on one box.
  `FlyMachinesRunner`   one ephemeral Fly machine per contribution. The shape the whole design
                   is for: a fresh container with its own filesystem, its own secrets, and
                   nothing of the previous worker in it.

The isolation argument, stated plainly, because it is the reason this seam exists at all. A
long-lived worker process — the model Temporal, Restate and every agent runtime assume — holds
credentials for everything it might ever be asked to do. One container per contribution means
the worker that reads the campaign database is a different container, in a different app, with
a different secret set, from the worker that writes to ClickUp. That is not a permission a
misconfiguration can widen; it is a boundary that has to be crossed to be violated.

The cost is honest too: a container boot plus an image pull is seconds. This is for cases that
live hours-to-days, and it is the wrong tool for a 200ms tool call.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..errors import ConfigurationError
from ..ports import RunState

# --------------------------------------------------------------------------- memory


class MemoryRunner:
    """Records starts; you say what became of them.

    The states a real platform produces at the worst moment — `GONE` for a machine that never
    booted, `EXITED` for one that ran and wrote nothing — are the ones that need testing, and
    they are exactly the ones that are hard to produce on demand against a real cloud. So they
    are settable here.
    """

    name = "memory"

    def __init__(self, default_state: RunState = RunState.RUNNING) -> None:
        self.started: List[Dict[str, Any]] = []
        self.stopped: List[str] = []
        self.states: Dict[str, RunState] = {}
        self.default_state = default_state
        self.fail_next: Optional[str] = None
        """Set to a message to make the next `start()` raise — the platform-refused path."""

        self._n = 0

    def start(self, *, image: str, cmd: Sequence[str], env: Dict[str, str], app: str = "",
              entrypoint: Sequence[str] = (), label: str = "",
              guest: Optional[Dict[str, Any]] = None, timeout_seconds: int = 600) -> str:
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise RuntimeError(msg)
        self._n += 1
        ref = f"mem-{self._n:04d}"
        self.started.append({"ref": ref, "image": image, "cmd": list(cmd), "env": dict(env),
                             "app": app, "entrypoint": list(entrypoint), "label": label,
                             "guest": guest or {}, "timeout_seconds": timeout_seconds})
        self.states[ref] = self.default_state
        return ref

    def state(self, ref: str) -> RunState:
        return self.states.get(ref, RunState.GONE)

    def stop(self, ref: str) -> None:
        self.stopped.append(ref)
        self.states[ref] = RunState.EXITED

    # -- test hooks ------------------------------------------------------

    def set_state(self, ref: str, state: RunState) -> None:
        self.states[ref] = state

    def env_of(self, ref: str) -> Dict[str, str]:
        return next((s["env"] for s in self.started if s["ref"] == ref), {})

    @property
    def last(self) -> Optional[Dict[str, Any]]:
        return self.started[-1] if self.started else None


# --------------------------------------------------------------------------- local


class LocalProcessRunner:
    """Run the worker as a subprocess here. Real, cheap, and not isolated — by admission.

    Useful for developing a worker and for a single-box deployment, and it makes the point that
    the case layer does not care what a "worker" is. What it does *not* give you is the
    property the design is about: a subprocess inherits this process's environment and
    filesystem, so `reach` is aspirational rather than enforced. Use it to build, then run the
    same worker under `FlyMachinesRunner` where the boundary is real.

    `image` is interpreted as the interpreter/entrypoint when `cmd` is empty, so a capability
    can declare `image="python:3.12-slim"` for Fly and be overridden locally with
    `image="python3"`.
    """

    name = "local"

    def __init__(self, *, cwd: Optional[str] = None, inherit_env: bool = False,
                 log_dir: Optional[str] = None) -> None:
        self.cwd = cwd
        self.inherit_env = inherit_env
        """False by default: the worker gets only what the dispatcher granted it. Turning this
        on hands it the whole ambient environment, which is convenient and is precisely the
        thing container isolation exists to stop."""

        self.log_dir = Path(log_dir) if log_dir else None
        self._procs: Dict[str, subprocess.Popen] = {}
        self._codes: Dict[str, int] = {}

    def start(self, *, image: str, cmd: Sequence[str], env: Dict[str, str], app: str = "",
              entrypoint: Sequence[str] = (), label: str = "",
              guest: Optional[Dict[str, Any]] = None, timeout_seconds: int = 600) -> str:
        argv = list(entrypoint) + list(cmd)
        if not argv:
            argv = shlex.split(image)
        if not argv:
            raise ConfigurationError("LocalProcessRunner needs a cmd or an executable image")
        full_env = dict(os.environ) if self.inherit_env else {}
        full_env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
        full_env.update(env)

        out = None
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            out = open(self.log_dir / f"{label or 'worker'}-{int(time.time())}.log", "wb")
        proc = subprocess.Popen(argv, cwd=self.cwd, env=full_env,
                                stdout=out or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT if out else subprocess.DEVNULL)
        ref = f"pid-{proc.pid}"
        self._procs[ref] = proc
        return ref

    def state(self, ref: str) -> RunState:
        proc = self._procs.get(ref)
        if proc is None:
            # A different dispatcher process started it. We cannot see it, and claiming GONE
            # would duplicate a healthy worker — so admit ignorance and let the caller extend.
            raise RuntimeError(f"{ref} was not started by this process — state unknown")
        code = proc.poll()
        if code is None:
            return RunState.RUNNING
        self._codes[ref] = code
        return RunState.EXITED if code == 0 else RunState.FAILED

    def stop(self, ref: str) -> None:
        proc = self._procs.get(ref)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def exit_code(self, ref: str) -> Optional[int]:
        return self._codes.get(ref)

    def wait(self, ref: str, timeout: float = 300) -> RunState:
        """Block until this worker finishes. Not part of the `Runner` protocol — the dispatcher
        never waits — but indispensable in a test or a compressed rehearsal."""
        proc = self._procs.get(ref)
        if proc is None:
            raise RuntimeError(f"unknown ref {ref}")
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return RunState.RUNNING
        return self.state(ref)


# --------------------------------------------------------------------------- fly.io


FLY_API = "https://api.machines.dev/v1"

_FLY_STATES = {
    # Fly machine states, mapped to what the dispatcher needs to decide.
    "created": RunState.RUNNING,
    "starting": RunState.RUNNING,
    "started": RunState.RUNNING,
    "replacing": RunState.RUNNING,
    "stopping": RunState.RUNNING,
    "stopped": RunState.EXITED,
    "suspended": RunState.EXITED,
    "destroying": RunState.EXITED,
    "destroyed": RunState.GONE,
    "failed": RunState.FAILED,
}


class FlyMachinesRunner:
    """One ephemeral Fly machine per contribution. Stdlib HTTP only — no SDK, no extra.

    The machine is created with `auto_destroy` and a `no` restart policy, so it runs once, exits,
    and removes itself. Billing is per second, which is what makes one-container-per-contribution
    reasonable rather than extravagant.

    On the two Fly-specific traps this deliberately avoids:

      **No `[http_service]`, no auto-start.** These machines are created already running and are
      never woken by traffic. Nothing routes to them.

      **`restart.policy = "no"`.** The default would restart a crashed worker *without* the
      dispatcher knowing, producing an attempt count that does not match reality. Restarts are
      the dispatcher's decision, because it is the only party that knows whether the case still
      needs the work.

    Secrets are the part to get right in production: set them on the *app* (`fly secrets set
    --app <app>`), one app per reach profile, and let the machine inherit them. Passing a
    credential through `env` at create time works and is visible in the machine config to anyone
    who can read the app — acceptable for a rehearsal, wrong for a standing deployment.
    """

    name = "fly"

    def __init__(self, *, token: Optional[str] = None, app: str = "",
                 region: str = "", api: str = FLY_API, timeout: float = 30.0,
                 opener: Any = None) -> None:
        self.token = token or _fly_token()
        if not self.token:
            raise ConfigurationError(
                "FlyMachinesRunner needs a token: pass token=, set FLY_API_TOKEN, or log in "
                "with flyctl (it is read from ~/.fly/config.yml)")
        self.default_app = app
        self.region = region
        self.api = api.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    # -- protocol --------------------------------------------------------

    def start(self, *, image: str, cmd: Sequence[str], env: Dict[str, str], app: str = "",
              entrypoint: Sequence[str] = (), label: str = "",
              guest: Optional[Dict[str, Any]] = None, timeout_seconds: int = 600) -> str:
        target = app or self.default_app
        if not target:
            raise ConfigurationError(
                "no Fly app for this capability — set Capability.app (preferred: one app per "
                "reach profile, secrets on the app) or FlyMachinesRunner(app=...)")
        g = {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512}
        g.update(guest or {})
        config: Dict[str, Any] = {
            "image": image,
            "env": {k: str(v) for k, v in env.items()},
            "auto_destroy": True,
            "restart": {"policy": "no"},
            "guest": g,
        }
        # `init` overrides the image's own entrypoint/cmd. Both are set explicitly because a
        # stock image reused as a worker host (postgres:16-alpine, python:3.12-slim) would
        # otherwise run its own default program and ignore the script entirely.
        init: Dict[str, Any] = {}
        if entrypoint:
            init["entrypoint"] = list(entrypoint)
        if cmd:
            init["cmd"] = list(cmd)
        if init:
            config["init"] = init
        body: Dict[str, Any] = {"config": config}
        if label:
            body["name"] = _fly_name(label)
        if self.region:
            body["region"] = self.region

        got = self._call("POST", f"/apps/{target}/machines", body)
        mid = got.get("id")
        if not mid:
            raise RuntimeError(f"fly create returned no machine id: {got}")
        # Encode the app in the ref: the dispatcher stores one opaque string and must be able
        # to ask about the machine later without re-deriving which app it lives in.
        return f"{target}/{mid}"

    def state(self, ref: str) -> RunState:
        app, mid = _split_ref(ref, self.default_app)
        try:
            got = self._call("GET", f"/apps/{app}/machines/{mid}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return RunState.GONE  # auto_destroy already reaped it
            raise
        raw = str(got.get("state") or "").lower()
        mapped = _FLY_STATES.get(raw, RunState.RUNNING)
        if mapped is RunState.EXITED:
            # A stopped machine that exited non-zero is a failure, and Fly reports the code on
            # the last exit event rather than on the machine. Treating a crash as a clean exit
            # would lose the distinction the dispatcher escalates on.
            if _fly_exit_code(got) not in (0, None):
                return RunState.FAILED
        return mapped

    def stop(self, ref: str) -> None:
        app, mid = _split_ref(ref, self.default_app)
        try:
            self._call("POST", f"/apps/{app}/machines/{mid}/stop", {})
        except urllib.error.HTTPError as e:
            if e.code not in (404, 412):
                raise

    # -- extras (not protocol, but you will want them) -------------------

    def destroy(self, ref: str) -> None:
        app, mid = _split_ref(ref, self.default_app)
        try:
            self._call("DELETE", f"/apps/{app}/machines/{mid}?force=true")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

    def wait_exit(self, ref: str, timeout: float = 300, poll: float = 3.0) -> RunState:
        """Block until the machine leaves a running state. For rehearsals, never the dispatcher."""
        deadline = time.time() + timeout
        last = RunState.RUNNING
        while time.time() < deadline:
            last = self.state(ref)
            if last is not RunState.RUNNING:
                return last
            time.sleep(poll)
        return last

    def ensure_app(self, app: str, *, org: str = "personal") -> bool:
        """Create the app if it does not exist. Returns True if it was created now.

        Convenience for standing a worker app up from code. Secrets still have to be set
        separately and deliberately — that is the one step which should not be automatic.
        """
        try:
            self._call("GET", f"/apps/{app}")
            return False
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        self._call("POST", "/apps", {"app_name": app, "org_slug": org})
        return True

    def logs(self, ref: str) -> str:
        """Best-effort recent output. Fly serves logs from a different host, so this is a
        convenience for debugging a rehearsal and not something to build on."""
        app, mid = _split_ref(ref, self.default_app)
        try:
            got = self._call("GET", f"/apps/{app}/machines/{mid}/events")
            return json.dumps(got, indent=2)[:8000]
        except Exception as e:  # noqa: BLE001
            return f"(logs unavailable: {e})"

    # -- http ------------------------------------------------------------

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        req = urllib.request.Request(
            f"{self.api}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        with self._opener(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}


# --------------------------------------------------------------------------- helpers


def _split_ref(ref: str, default_app: str) -> "tuple[str, str]":
    if "/" in ref:
        app, mid = ref.split("/", 1)
        return app, mid
    return default_app, ref


def _fly_name(label: str) -> str:
    """Fly machine names must be DNS-ish. A rejected name would fail the whole dispatch, so
    normalize rather than trust the caller's label."""
    safe = "".join(c if (c.isalnum() or c == "-") else "-" for c in label.lower())
    return safe.strip("-")[:58] or "abeyance-worker"


def _fly_exit_code(machine: Dict[str, Any]) -> Optional[int]:
    for ev in machine.get("events") or []:
        code = ((ev.get("request") or {}).get("exit_event") or {}).get("exit_code")
        if code is not None:
            return int(code)
    return None


def _fly_token() -> str:
    """FLY_API_TOKEN, else the token flyctl already stored. Reading flyctl's config means a
    developer who is logged in does not need a second credential to run a rehearsal."""
    tok = os.environ.get("FLY_API_TOKEN") or os.environ.get("FLY_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    cfg = Path.home() / ".fly" / "config.yml"
    if not cfg.exists():
        return ""
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("access_token:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""
