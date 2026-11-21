"""Case store: one JSON file per case, plus an in-memory cache.

Persistence only. Stage changes and gate checks live in the state machine
(F1); the store just saves what it is given. Doctor decisions are written to
the audit log here, since every one of them passes through `append_decision`.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import AwareDatetime, Field

from agent.audit import DATA_DIR, audit
from agent.schemas import (
    Bucket,
    CaseState,
    ClassifierOutput,
    DoctorDecision,
    DoctorResults,
    MutationCheckOutput,
    NoMatchPlan,
    PreOpScreen,
    Schema,
    Shortlist,
)

_CASE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # also blocks path traversal


class CaseNotFound(KeyError):
    pass


class CaseExists(ValueError):
    pass


class CaseRecord(Schema):
    """Everything the agent knows about one case. Agent internal."""

    state: CaseState
    created_at: AwareDatetime
    classifier: ClassifierOutput | None = None
    decisions: list[DoctorDecision] = Field(default_factory=list)
    results: DoctorResults | None = None
    preop: PreOpScreen | None = None
    mutation: MutationCheckOutput | None = None
    shortlist: Shortlist | None = None
    no_match: NoMatchPlan | None = None

    @property
    def case_id(self) -> str:
        return self.state.case_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CaseStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, CaseRecord] = {}
        self._lock = threading.RLock()

    def _path(self, case_id: str) -> Path:
        if not _CASE_ID.match(case_id):
            raise ValueError(f"bad case_id {case_id!r}: use letters, digits, - or _")
        return self.root / f"{case_id}.json"

    def _write(self, record: CaseRecord) -> None:
        path = self._path(record.case_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: readers never see half a file
        self._cache[record.case_id] = record

    # -- public API ---------------------------------------------------------

    def create(self, case_id: str, suspected_bucket: Bucket | None = None) -> CaseRecord:
        with self._lock:
            if case_id in self._cache or self._path(case_id).exists():
                raise CaseExists(case_id)
            now = _now()
            record = CaseRecord(
                state=CaseState(
                    case_id=case_id,
                    stage="intake",
                    suspected_bucket=suspected_bucket,
                    waiting_on="agent",
                    updated_at=now,
                ),
                created_at=now,
            )
            self._write(record)
        audit("agent", "case_created", {"suspected_bucket": suspected_bucket}, case_id=case_id)
        return record.model_copy(deep=True)

    def get(self, case_id: str) -> CaseRecord:
        """A copy of the case. Change it, then pass it to `save`."""
        with self._lock:
            record = self._cache.get(case_id)
            if record is None:
                path = self._path(case_id)
                if not path.exists():
                    raise CaseNotFound(case_id)
                record = CaseRecord.model_validate_json(path.read_text(encoding="utf-8"))
                self._cache[case_id] = record
            return record.model_copy(deep=True)

    def save(self, record: CaseRecord) -> CaseRecord:
        with self._lock:
            if record.case_id not in self._cache and not self._path(record.case_id).exists():
                raise CaseNotFound(record.case_id)
            record = record.model_copy(deep=True)
            record.state.updated_at = _now()
            # Re-validate so in-place edits can't sneak bad data to disk.
            record = CaseRecord.model_validate(record.model_dump())
            self._write(record)
            return record.model_copy(deep=True)

    def append_decision(self, decision: DoctorDecision) -> CaseRecord:
        """Record a doctor decision and audit it (rules 2 and 7).

        Audit first, under the same lock: if the audit write fails, nothing is
        saved, so a decision can never exist without its audit entry.
        """
        with self._lock:
            record = self.get(decision.case_id)
            record.decisions.append(decision)
            audit(
                "doctor",
                f"decision:{decision.gate}",
                {"doctor_id": decision.doctor_id, "decision": decision.decision, "note": decision.note},
                case_id=decision.case_id,
            )
            try:
                return self.save(record)
            except Exception as exc:
                audit(
                    "agent",
                    "decision_save_failed",
                    {"gate": decision.gate, "error": f"{type(exc).__name__}: {exc}"},
                    case_id=decision.case_id,
                )
                raise

    def list_cases(self) -> list[CaseState]:
        with self._lock:
            ids = {p.stem for p in self.root.glob("*.json")} | set(self._cache)
            return sorted((self.get(i).state for i in ids), key=lambda s: s.updated_at, reverse=True)

    def reset(self) -> int:
        """Delete every case (demo reset). Returns how many were removed."""
        with self._lock:
            files = list(self.root.glob("*.json"))
            for p in files:
                p.unlink()
            self._cache.clear()
        audit("system", "cases_reset", {"removed": len(files)})
        return len(files)


_default = CaseStore(DATA_DIR / "cases")


def get_store() -> CaseStore:
    return _default


def set_store(store: CaseStore) -> None:
    """Swap the process-wide store (tests, demo)."""
    global _default
    _default = store
