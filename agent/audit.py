"""Append-only audit log (safety rule 7).

Every agent action, tool call and doctor approval goes through `audit()`.
One JSON line per entry in `<data dir>/audit.jsonl`, or, during an API
request, in that request's workspace log (`agent.workspace`). Nothing here
edits or deletes lines.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic_core import to_jsonable_python

from agent.schemas import AuditEntry

Actor = Literal["agent", "doctor", "system"]
F = TypeVar("F", bound=Callable[..., Any])

DATA_DIR = Path(os.environ.get("CORTEXA_DATA_DIR", Path(__file__).parent / "data"))
_MAX_VALUE_CHARS = 500


def _jsonable(value: Any) -> Any:
    """JSON-safe copy of a value; unknown types become their str()."""
    return to_jsonable_python(value, fallback=str)


def _short(value: Any) -> Any:
    """Keep log lines small: long values are cut, lists show their length."""
    value = _jsonable(value)
    text = json.dumps(value)
    if len(text) <= _MAX_VALUE_CHARS:
        return value
    if isinstance(value, list):
        return {"type": "list", "len": len(value)}
    return text[:_MAX_VALUE_CHARS] + "…"


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(
        self,
        actor: Actor,
        action: str,
        detail: dict[str, Any] | None = None,
        case_id: str | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            ts=datetime.now(timezone.utc),
            case_id=case_id,
            actor=actor,
            action=action,
            detail=_jsonable(detail or {}),
        )
        line = entry.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        return entry

    def read(self, case_id: str | None = None) -> list[AuditEntry]:
        """All entries in order, or only one case's entries."""
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        entries = [AuditEntry.model_validate_json(line) for line in lines if line.strip()]
        if case_id is not None:
            entries = [e for e in entries if e.case_id == case_id]
        return entries


_default = AuditLog(DATA_DIR / "audit.jsonl")
# Set by agent.workspace.Workspace.active() for one API request (issue 48).
scoped_log: ContextVar[AuditLog | None] = ContextVar("cortexa_audit_log", default=None)


def get_audit_log() -> AuditLog:
    """The current workspace's log during a request, else the process-wide one."""
    return scoped_log.get() or _default


def set_audit_log(log: AuditLog) -> None:
    """Swap the process-wide log (tests, demo reset)."""
    global _default
    _default = log


def audit(
    actor: Actor,
    action: str,
    detail: dict[str, Any] | None = None,
    case_id: str | None = None,
) -> AuditEntry:
    return get_audit_log().append(actor, action, detail, case_id)


def audited(action: str) -> Callable[[F], F]:
    """Log a tool call: its arguments, then its result or error and duration.

    If the wrapped function has a `case_id` argument, the entry is filed under it.
    Errors are logged and re-raised, never swallowed.
    """

    def wrap(fn: F) -> F:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(*args, **kwargs)
            call_args = {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
            case_id = call_args.get("case_id")
            detail: dict[str, Any] = {"args": {k: _short(v) for k, v in call_args.items()}}
            start = time.perf_counter()

            def elapsed() -> float:
                return round((time.perf_counter() - start) * 1000, 1)

            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                detail.update(ok=False, error=f"{type(exc).__name__}: {exc}", ms=elapsed())
                try:
                    audit("agent", action, detail, case_id=case_id)
                except Exception as audit_exc:
                    # Keep the tool's error; attach the audit failure to it.
                    exc.add_note(f"audit write also failed: {type(audit_exc).__name__}: {audit_exc}")
                raise
            # Success: an audit failure here must surface (rule 7), so no try.
            detail.update(ok=True, result=_short(result), ms=elapsed())
            audit("agent", action, detail, case_id=case_id)
            return result

        return inner  # type: ignore[return-value]

    return wrap
