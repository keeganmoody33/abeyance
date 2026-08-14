"""Where a proposal lives while nothing is running.

Three implementations, and the choice between them is a real architectural decision rather
than a preference:

  `MemoryStore`    tests only. Dies with the process, which is exactly what the library is
                   built to survive.
  `JSONFileStore`  one file per document on local disk. Fine when exactly one machine will
                   ever run this loop.
  `PostgresStore`  shared, and the right default the moment a second host exists.

The failure mode that makes this a decision: per-host state is not merely inconvenient when
two machines run the same loop, it is *silently wrong*. Each host reads its own cursors, so a
machine that has stopped running the loop keeps a frozen snapshot forever and cannot tell
"nothing happened" from "the other host already handled it". The symptom is a digest
confidently reporting work as outstanding that was done and applied days ago, and there is no
error anywhere to notice. Filesystem state makes the *host* authoritative when the *record*
should be.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional


class MemoryStore:
    """In-process dict. For tests and examples."""

    def __init__(self) -> None:
        self._d: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            doc = self._d.get(kind, {}).get(key)
            return json.loads(json.dumps(doc)) if doc is not None else None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        with self._lock:
            self._d.setdefault(kind, {})[key] = json.loads(json.dumps(doc))

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._d.get(kind, {})))

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._d.get(kind, {}).pop(key, None)


class JSONFileStore:
    """One JSON file per document under `root/<kind>/<key>.json`.

    Writes are atomic (temp file plus rename) because the alternative — a half-written
    proposal after a crash mid-write — loses the approvals already recorded in it, and those
    are the one thing in the system that cannot be regenerated.
    """

    def __init__(self, root: os.PathLike | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, kind: str) -> Path:
        d = self.root / _safe(kind)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, kind: str, key: str) -> Path:
        return self._dir(kind) / f"{_safe(key)}.json"

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        p = self._path(kind, key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        p = self._path(kind, key)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for f in sorted(self._dir(kind).glob("*.json")):
            try:
                out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
        return out

    def delete(self, kind: str, key: str) -> None:
        self._path(kind, key).unlink(missing_ok=True)


class PostgresStore:
    """Shared state in one `(kind, key) -> jsonb` table. Requires the `postgres` extra.

    The table is created on first use rather than through a migration step, because several
    hosts run this code and a loop that needs a manual migration before it can start is a loop
    that will one day not start. Every write stamps `updated_by` with the host or container id,
    so "which machine last touched this" is answerable — the provenance a filesystem layout
    used to imply by location and a shared table has to record explicitly.
    """

    def __init__(self, dsn: str, *, schema: str = "abeyance", table: str = "state",
                 connect: Any = None) -> None:
        self.dsn = dsn
        self.schema = schema
        self.table = table
        self._connect = connect or _psycopg_connect()
        self._ensured = False

    @property
    def _qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    def _host(self) -> str:
        app, mach = os.environ.get("FLY_APP_NAME"), os.environ.get("FLY_MACHINE_ID")
        if app:
            return f"fly:{app}/{mach or '?'}"
        for var in ("HOSTNAME", "K_SERVICE", "DYNO"):
            if os.environ.get(var):
                return os.environ[var]
        try:
            return socket.gethostname()
        except Exception:  # noqa: BLE001
            return "unknown"

    def _ensure(self, cur: Any) -> None:
        if self._ensured:
            return
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {self.schema}")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self._qualified} (
                kind       text        NOT NULL,
                key        text        NOT NULL,
                doc        jsonb       NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now(),
                updated_by text,
                PRIMARY KEY (kind, key)
            )""")
        self._ensured = True

    def _run(self, sql: str, params: tuple = (), *, fetch: bool = False) -> Any:
        with self._connect(self.dsn) as conn:
            with conn.cursor() as cur:
                self._ensure(cur)
                cur.execute(sql, params)
                rows = cur.fetchall() if fetch else None
            conn.commit()
        return rows

    def get(self, kind: str, key: str) -> Optional[Dict[str, Any]]:
        rows = self._run(f"SELECT doc FROM {self._qualified} WHERE kind=%s AND key=%s",
                         (kind, key), fetch=True)
        return _as_doc(rows[0][0]) if rows else None

    def put(self, kind: str, key: str, doc: Dict[str, Any]) -> None:
        self._run(
            f"""INSERT INTO {self._qualified} (kind, key, doc, updated_at, updated_by)
                VALUES (%s, %s, %s::jsonb, now(), %s)
                ON CONFLICT (kind, key)
                DO UPDATE SET doc = EXCLUDED.doc,
                              updated_at = now(),
                              updated_by = EXCLUDED.updated_by""",
            (kind, key, json.dumps(doc), self._host()))

    def items(self, kind: str) -> Dict[str, Dict[str, Any]]:
        rows = self._run(f"SELECT key, doc FROM {self._qualified} WHERE kind=%s ORDER BY key",
                         (kind,), fetch=True) or []
        return {r[0]: _as_doc(r[1]) for r in rows}

    def delete(self, kind: str, key: str) -> None:
        self._run(f"DELETE FROM {self._qualified} WHERE kind=%s AND key=%s", (kind, key))


# --------------------------------------------------------------------------- helpers


def _safe(s: str) -> str:
    """Filesystem-safe fragment. Keys are ids from a transport, so they can carry anything."""
    return "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in str(s))[:180] or "_"


def _as_doc(v: Any) -> Dict[str, Any]:
    return json.loads(v) if isinstance(v, (str, bytes)) else v


def _psycopg_connect():
    try:
        import psycopg  # type: ignore
        return psycopg.connect
    except ImportError:  # pragma: no cover
        try:
            import psycopg2  # type: ignore
            return psycopg2.connect
        except ImportError:
            raise ImportError(
                "PostgresStore needs psycopg (or psycopg2). Install the extra:\n"
                '    pip install "abeyance[postgres]"\n'
                "or pass your own connect callable: PostgresStore(dsn, connect=my_connect)"
            ) from None
