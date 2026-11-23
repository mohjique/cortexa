"""Per-browser workspaces for the public demo (issue 48).

Each browser makes a random workspace ID, keeps it in localStorage and sends
it on every API call (header `X-Cortexa-Workspace`). Its cases, audit log,
stored scans and heatmaps live under

    <CORTEXA_DATA_DIR>/workspaces/<workspace_id>/
        workspace.json   created_at; its mtime is the last-used time
        cases/           one JSON file per case (agent.store.CaseStore)
        audit.jsonl      this workspace's audit log (agent.audit.AuditLog)
        scans/           re-encoded uploads, for the doctor's viewer
        heatmaps/        Grad-CAM overlays

This is isolation, not authentication. Anyone who holds a workspace ID can
open that workspace. It is acceptable only because every case is synthetic
(safety rule 9); it keeps one demo visitor from seeing or changing another
visitor's cases, nothing more.

`Workspace.active()` points `agent.store.get_store()` and
`agent.audit.audit()` at the workspace for the current context, so the
workflow, matching, pre-op and eligibility code write to the right place
without knowing workspaces exist. There is no background work today; any
added later must run inside `contextvars.copy_context()` (or
`Workspace.active()`) so it keeps the workspace.

Housekeeping, so the public Space can't fill its disk: when a new workspace
is created, workspaces unused for more than `WORKSPACE_TTL_HOURS` are
deleted, then the least recently used ones until fewer than `MAX_WORKSPACES`
remain. Both are env vars.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent import audit as audit_mod
from agent import store as store_mod
from agent.audit import DATA_DIR, AuditLog
from agent.store import CaseStore

WORKSPACE_HEADER = "X-Cortexa-Workspace"
# Only the two case image routes read the ID from the query string, because
# an <img src> cannot send a header. Everything else needs the header.
WORKSPACE_QUERY = "ws"
# Letters, digits, - and _ only: no dots or slashes, so no path traversal.
_WS_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAX_WORKSPACES = int(os.environ.get("CORTEXA_MAX_WORKSPACES", "200"))
WORKSPACE_TTL_HOURS = float(os.environ.get("CORTEXA_WORKSPACE_TTL_HOURS", "24"))
_TOUCH_EVERY_S = 60.0  # write the last-used time at most once a minute per workspace
_MARKER = "workspace.json"


class InvalidWorkspace(ValueError):
    pass


def validate_id(raw: str | None) -> str:
    """The workspace ID, lower-cased, or InvalidWorkspace.

    Lower-cased so two spellings can't map to one folder on a case-insensitive
    disk while looking like two workspaces in memory.
    """
    if raw is None or not _WS_ID.match(raw):
        raise InvalidWorkspace(
            f"send a workspace ID in the {WORKSPACE_HEADER} header: 8-64 letters, digits, - or _"
        )
    return raw.lower()


@dataclass(eq=False)
class Workspace:
    id: str
    root: Path
    store: CaseStore = field(init=False)
    log: AuditLog = field(init=False)

    def __post_init__(self) -> None:
        self.store = CaseStore(self.root / "cases")
        self.log = AuditLog(self.root / "audit.jsonl")

    @property
    def scan_dir(self) -> Path:
        return self.root / "scans"

    @property
    def heatmap_dir(self) -> Path:
        return self.root / "heatmaps"

    @contextmanager
    def active(self) -> Iterator[Workspace]:
        """Route get_store() and audit() to this workspace for the current context."""
        s = store_mod.scoped_store.set(self.store)
        a = audit_mod.scoped_log.set(self.log)
        try:
            yield self
        finally:
            audit_mod.scoped_log.reset(a)
            store_mod.scoped_store.reset(s)


class Registry:
    """Opens, seeds, resets and evicts workspaces. Thread-safe."""

    def __init__(
        self,
        root: Path,
        max_workspaces: int = MAX_WORKSPACES,
        ttl_hours: float = WORKSPACE_TTL_HOURS,
    ) -> None:
        if max_workspaces < 1:
            raise ValueError("max_workspaces must be at least 1")
        self.root = Path(root)
        self.max_workspaces = max_workspaces
        self.ttl_s = ttl_hours * 3600
        self._lock = threading.RLock()
        self._open: dict[str, Workspace] = {}
        self._touched: dict[str, float] = {}

    def _dir(self, ws_id: str) -> Path:
        return self.root / validate_id(ws_id)

    def open(self, ws_id: str) -> tuple[Workspace, bool]:
        """The workspace, and whether it was just created (and seeded).

        Idempotent and safe under concurrent first requests: creation runs
        under the registry lock and checks the marker file first.
        """
        ws_id = validate_id(ws_id)
        with self._lock:
            ws = self._open.get(ws_id)
            created = False
            if ws is None:
                root = self._dir(ws_id)
                if not (root / _MARKER).is_file():
                    self.evict(keep=ws_id, room_for_one=True)
                    _seed(root)
                    created = True
                ws = self._open[ws_id] = Workspace(ws_id, root)
            self._touch(ws)
            return ws, created

    def get(self, ws_id: str) -> Workspace:
        return self.open(ws_id)[0]

    def reset(self, ws_id: str) -> tuple[Workspace, int]:
        """Delete the workspace's cases, audit log and images, then seed it again.

        Returns the fresh workspace and how many cases were removed.
        """
        ws_id = validate_id(ws_id)
        with self._lock:
            old = self._open.pop(ws_id, None)
            root = self._dir(ws_id)
            removed = len(list((root / "cases").glob("*.json")))
            if old is not None:
                with old.store._lock:  # no case write half-way through the delete
                    _remove(root)
            else:
                _remove(root)
            self._touched.pop(ws_id, None)
            ws, _ = self.open(ws_id)
            return ws, removed

    def evict(self, keep: str | None = None, room_for_one: bool = False) -> list[str]:
        """Delete stale workspaces, then the least recently used beyond the cap."""
        with self._lock:
            now = time.time()
            ages = {wid: now - t for wid, t in self._last_used().items() if wid != keep}
            gone = [wid for wid, age in ages.items() if age > self.ttl_s]
            left = sorted((w for w in ages if w not in gone), key=lambda w: ages[w])  # newest first
            cap = self.max_workspaces - (1 if room_for_one else 0) - (1 if keep and self._exists(keep) else 0)
            if len(left) > max(cap, 0):
                gone += left[max(cap, 0):]
            for wid in gone:
                ws = self._open.pop(wid, None)
                self._touched.pop(wid, None)
                if ws is not None:
                    with ws.store._lock:
                        _remove(self.root / wid)
                else:
                    _remove(self.root / wid)
            return gone

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._last_used())

    # -- internals ----------------------------------------------------------

    def _exists(self, ws_id: str) -> bool:
        return (self.root / ws_id / _MARKER).is_file()

    def _last_used(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if not self.root.is_dir():
            return out
        for d in self.root.iterdir():
            if not d.is_dir() or not _WS_ID.match(d.name):
                continue
            marker = d / _MARKER
            try:
                t = marker.stat().st_mtime if marker.exists() else d.stat().st_mtime
            except OSError:
                continue
            out[d.name] = max(t, self._touched.get(d.name, 0.0))
        return out

    def _touch(self, ws: Workspace) -> None:
        now = time.time()
        if now - self._touched.get(ws.id, 0.0) < _TOUCH_EVERY_S:
            return
        self._touched[ws.id] = now
        try:
            os.utime(ws.root / _MARKER, None)
        except OSError:
            pass  # in-memory time still counts


def _seed(root: Path) -> None:
    """A fresh workspace, in the form the app starts every visitor today.

    The backend has never pre-created demo cases: the 23 demo MRIs are a
    shared, read-only gallery (GET /demo-scans), and the doctor opens one as a
    case by uploading it. So a new workspace starts with no cases and the full
    demo gallery. If server-side demo cases are ever added, create them here,
    once, so each workspace gets its own copy.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases").mkdir(exist_ok=True)
    marker = root / _MARKER
    tmp = root / (_MARKER + ".tmp")
    tmp.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    os.replace(tmp, marker)  # the marker appears last: a half-made workspace is seeded again


def _remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


_registry = Registry(DATA_DIR / "workspaces")


def get_registry() -> Registry:
    return _registry


def set_registry(registry: Registry) -> None:
    """Swap the process-wide registry (tests)."""
    global _registry
    _registry = registry
