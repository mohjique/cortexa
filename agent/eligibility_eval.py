"""F8.6: agreement between the eligibility checker and the gold labels.

Gold labels are the premed teammate's manual calls in
`clinical/patients/demo-*.json` (`gold_eligibility`), against the synthetic
trials in `agent/demo_cache/`. Run from the repo root:

    python -m agent.eligibility_eval            # summary + disagreements
    python -m agent.eligibility_eval --json     # machine-readable

Two input modes, because the patient files' `enter_results` blocks leave
out some optional fields the gold labels were written from:

- `entered`: the doctor-entered results exactly as the demo submits them.
  Fields left out are `None`, so dependent criteria are `unknown` (rule 3).
- `profile`: the same, with `recurrence_number`, `comorbidities`,
  `can_use_phone_or_tablet` and `tmb_clia` filled in from the profile.

Cases still before pathology (pre-op screen) are checked with pre-op facts
from the profile: age, radiology report, prior treatments.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent.eligibility import Facts, check_trial
from agent.schemas import DoctorResults, PriorTreatment, TrialRecord
from agent.trials import search_trials

PATIENTS_DIR = Path(__file__).resolve().parents[1] / "clinical" / "patients"
PROFILE_FILLS = ("recurrence_number", "comorbidities", "can_use_phone_or_tablet", "tmb_clia")
Mode = Literal["entered", "profile"]


def load_patients() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(PATIENTS_DIR.glob("demo-*.json"))]


def facts_for(patient: dict[str, Any], mode: Mode) -> Facts:
    entered = patient.get("doctor_decisions", {}).get("enter_results")
    bucket = patient.get("doctor_decisions", {}).get("confirm_bucket", {}).get("bucket")
    if entered is None:
        return Facts(
            stage="preop",
            bucket=bucket,
            age=patient.get("age"),
            recurrent=patient.get("recurrent"),
            recurrence_number=patient.get("recurrence_number"),
            prior_treatments=tuple(PriorTreatment(**t) for t in patient.get("prior_treatments", [])),
            comorbidities=tuple(patient.get("comorbidities") or ()),
            can_use_phone_or_tablet=patient.get("can_use_phone_or_tablet"),
            radiology_report=patient.get("radiology_report"),
        )
    data = dict(entered)
    if mode == "profile":
        for k in PROFILE_FILLS:
            if data.get(k) is None and patient.get(k) is not None:
                data[k] = patient[k]
    return Facts.from_results(DoctorResults(**data))


@dataclass
class Disagreement:
    case_id: str
    trial_id: str
    criterion_text: str
    gold: str
    got: str
    gold_field: str | None
    got_field: str
    reason: str


@dataclass
class Report:
    mode: Mode
    rows: int = 0
    status_agree: int = 0
    field_agree: int = 0
    missing: int = 0  # gold rows the checker produced no row for
    by_status: dict[str, dict[str, int]] = field(default_factory=dict)  # gold -> got -> n
    disagreements: list[Disagreement] = field(default_factory=list)

    @property
    def status_rate(self) -> float:
        return self.status_agree / self.rows if self.rows else 0.0


def run(mode: Mode, trials: dict[str, TrialRecord] | None = None) -> Report:
    trials = trials or {t.trial_id: t for t in search_trials()}
    rep = Report(mode=mode)
    confusion: dict[str, Counter[str]] = {}
    for p in load_patients():
        gold = p.get("gold_eligibility") or []
        if not gold:
            continue
        facts = facts_for(p, mode)
        got: dict[tuple[str, str], Any] = {}
        for tid in sorted({g["trial_id"] for g in gold}):
            for row in check_trial(trials[tid], facts, case_id=p["case_id"]):
                got[(tid, row.criterion_text)] = row
        for g in gold:
            rep.rows += 1
            row = got.get((g["trial_id"], g["criterion_text"]))
            if row is None:
                rep.missing += 1
                confusion.setdefault(g["status"], Counter())["(no row)"] += 1
                continue
            confusion.setdefault(g["status"], Counter())[row.status] += 1
            if row.evidence_field == g.get("evidence_field"):
                rep.field_agree += 1
            if row.status == g["status"]:
                rep.status_agree += 1
            else:
                rep.disagreements.append(Disagreement(
                    p["case_id"], g["trial_id"], g["criterion_text"], g["status"], row.status,
                    g.get("evidence_field"), row.evidence_field, row.reason,
                ))
    rep.by_status = {k: dict(v) for k, v in confusion.items()}
    return rep


def _print(rep: Report) -> None:
    print(f"\n== mode: {rep.mode} ==")
    print(f"status agreement: {rep.status_agree}/{rep.rows} = {rep.status_rate:.1%}"
          f"   evidence field agreement: {rep.field_agree}/{rep.rows}   missing rows: {rep.missing}")
    print("gold -> checker:", rep.by_status)
    for d in rep.disagreements:
        print(f"  {d.case_id} {d.trial_id} gold={d.gold} got={d.got} [{d.got_field}] "
              f"{d.criterion_text!r}: {d.reason}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="F8.6 eligibility agreement vs gold labels")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    reports = [run("entered"), run("profile")]
    if args.json:
        out = [asdict(r) | {"status_rate": r.status_rate} for r in reports]
        json.dump(out, sys.stdout, indent=2)
        print()
    else:
        for r in reports:
            _print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
