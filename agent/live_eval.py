"""Checker vs hand labels on live ClinicalTrials.gov criteria text (issue 48).

Fixtures: `agent/tests/fixtures/live_criteria/NCT*.json` (public registry
text) and `labels.json` (hand labels per bullet for two synthetic profiles).
Run from the repo root:

    python -m agent.live_eval              # per-profile summary
    python -m agent.live_eval -v           # plus every false met
    python -m agent.live_eval --json

A **false met** is a checker `met` where the hand label is `not_met` or
`unknown`. It is the error that matters most: the doctor would see a pass the
patient's record does not support (safety rules 1 and 3). The target is zero.

`false_met_rate` = false mets / checker `met` rows: the share of the
checker's passes that are wrong.

A **false not_met** is a checker `not_met` where the label is `met` or
`unknown`. It hides the trial from the shortlist. The target is also zero.

**Guard unknowns** are rows the not-met guard turned `unknown`; those labeled
`not_met` are its cost (correct failures sent to doctor review).

    python -m agent.live_eval --parses F   # with LLM parses from F (issue 56)

Bullets are checked one at a time with `evaluate()`, offline. This is an
evaluation, not an agent action, so nothing is written to the audit log.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent.criteria_parse import PARSED_PREFIX, ParseSource, dict_source, evaluate_with_parse
from agent.eligibility import NOT_MET_DOUBT_MARKER, Facts, split_criteria, to_row
from agent.policy import Policy, get_policy
from agent.schemas import DoctorResults

FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "live_criteria"
LABELS_FILE = FIXTURE_DIR / "labels.json"

Key = tuple[str, int]  # (trial_id, bullet index from split_criteria)


@dataclass(frozen=True)
class LiveRow:
    """One checker call on one bullet."""

    trial_id: str
    index: int
    criterion_type: str
    criterion_text: str
    status: str
    evidence_field: str
    reason: str

    @property
    def key(self) -> Key:
        return (self.trial_id, self.index)


@dataclass(frozen=True)
class WrongCall:
    """A checker row whose status the hand label contradicts (false met or false not_met)."""

    row: LiveRow
    label: str


def load_fixtures(directory: Path = FIXTURE_DIR) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("NCT*.json"))}


def load_labels(path: Path = LABELS_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def profile_facts(labels: Mapping[str, Any], profile: str) -> Facts:
    return Facts.from_results(DoctorResults(**labels["profiles"][profile]))


def labels_for(labels: Mapping[str, Any], profile: str) -> dict[Key, str]:
    """{(trial_id, index): gold status} for one profile."""
    return {
        (tid, b["index"]): b["labels"][profile]
        for tid, bullets in labels["trials"].items()
        for b in bullets
    }


def check_fixture(fixture: Mapping[str, Any], facts: Facts, policy: Policy | None = None,
                  parses: ParseSource | None = None) -> list[LiveRow]:
    """The checker's call on every bullet of one fixture, in split order.

    `parses` (issue 56): refine each bullet with its LLM parse, as `check_trial` does.
    """
    policy = policy or get_policy()
    tid = fixture["trial_id"]
    rows = []
    for i, c in enumerate(split_criteria(fixture["criteria_text"])):
        r = to_row(tid, c, evaluate_with_parse(c, facts, policy, parses))
        rows.append(LiveRow(tid, i, c.type, c.text, r.status, r.evidence_field, r.reason))
    return rows


def run_profile(profile: str, fixtures: Mapping[str, Mapping[str, Any]] | None = None,
                labels: Mapping[str, Any] | None = None, policy: Policy | None = None,
                parses: ParseSource | None = None) -> list[LiveRow]:
    fixtures = fixtures if fixtures is not None else load_fixtures()
    labels = labels if labels is not None else load_labels()
    facts = profile_facts(labels, profile)
    return [row for tid in sorted(labels["trials"]) for row in check_fixture(fixtures[tid], facts, policy, parses)]


def _check_alignment(rows: Iterable[LiveRow], texts: Mapping[Key, str] | None) -> None:
    if texts is None:
        return
    for r in rows:
        if r.key in texts and texts[r.key] != r.criterion_text:
            raise ValueError(f"label text for {r.key} does not match the split bullet; regenerate labels")


def false_met(rows: Iterable[LiveRow], labels: Mapping[Key, str]) -> list[WrongCall]:
    """Checker `met` where the label is `not_met` or `unknown`. Unlabeled rows are skipped."""
    out = []
    for r in rows:
        gold = labels.get(r.key)
        if r.status == "met" and gold in ("not_met", "unknown"):
            out.append(WrongCall(r, gold))
    return out


def false_not_met(rows: Iterable[LiveRow], labels: Mapping[Key, str]) -> list[WrongCall]:
    """Checker `not_met` where the label is `met` or `unknown`.

    A wrong not_met hides the trial from the shortlist, so it is the other
    safety number next to false met (issue 48 live dry run).
    """
    out = []
    for r in rows:
        gold = labels.get(r.key)
        if r.status == "not_met" and gold in ("met", "unknown"):
            out.append(WrongCall(r, gold))
    return out


def guard_unknown(rows: Iterable[LiveRow], labels: Mapping[Key, str]) -> tuple[int, int]:
    """(labeled rows the not-met guard turned unknown, of those labeled not_met).

    The second number is the guard's cost: correct not_mets it sent to doctor
    review. It is the baseline the LLM criteria parser (#56) should lower.
    """
    rows = [r for r in rows if r.key in labels and NOT_MET_DOUBT_MARKER in r.reason]
    return len(rows), sum(labels[r.key] == "not_met" for r in rows)


def false_met_rate(rows: Iterable[LiveRow], labels: Mapping[Key, str]) -> float:
    """False mets / checker `met` rows on labeled bullets (0.0 when there are none)."""
    rows = [r for r in rows if r.key in labels]
    mets = sum(r.status == "met" for r in rows)
    return len(false_met(rows, labels)) / mets if mets else 0.0


def agreement(rows: Iterable[LiveRow], labels: Mapping[Key, str]) -> tuple[int, int]:
    """(rows whose status equals the label, labeled rows)."""
    rows = [r for r in rows if r.key in labels]
    return sum(r.status == labels[r.key] for r in rows), len(rows)


@dataclass
class ProfileReport:
    profile: str
    bullets: int
    checker_met: int
    false_met: int
    false_met_rate: float
    agree: int
    agreement_rate: float
    confusion: dict[str, dict[str, int]]  # label -> checker -> n
    false_not_met: int = 0
    guard_unknown: int = 0  # rows the not-met guard turned unknown
    guard_unknown_labeled_not_met: int = 0  # of those, labeled not_met
    label_met: int = 0          # bullets labeled met
    label_met_as_met: int = 0   # ... that the checker calls met (recall, issue 56)
    decided_by_parse: int = 0   # rows whose call came from an LLM parse


def report(profile: str, fixtures: Mapping[str, Mapping[str, Any]] | None = None,
           labels: Mapping[str, Any] | None = None,
           parses: ParseSource | None = None) -> tuple[ProfileReport, list[WrongCall]]:
    labels = labels if labels is not None else load_labels()
    rows = run_profile(profile, fixtures, labels, parses=parses)
    gold = labels_for(labels, profile)
    texts = {(tid, b["index"]): b["text"] for tid, bs in labels["trials"].items() for b in bs}
    _check_alignment(rows, texts)
    fm = false_met(rows, gold)
    agree, n = agreement(rows, gold)
    guarded, guarded_not_met = guard_unknown(rows, gold)
    confusion: dict[str, dict[str, int]] = {}
    for r in rows:
        if r.key in gold:
            cell = confusion.setdefault(gold[r.key], {})
            cell[r.status] = cell.get(r.status, 0) + 1
    rep = ProfileReport(
        profile=profile,
        bullets=n,
        checker_met=sum(r.status == "met" for r in rows if r.key in gold),
        false_met=len(fm),
        false_met_rate=false_met_rate(rows, gold),
        agree=agree,
        agreement_rate=agree / n if n else 0.0,
        confusion=confusion,
        false_not_met=len(false_not_met(rows, gold)),
        guard_unknown=guarded,
        guard_unknown_labeled_not_met=guarded_not_met,
        label_met=sum(v == "met" for v in gold.values()),
        label_met_as_met=confusion.get("met", {}).get("met", 0),
        decided_by_parse=sum(r.reason.startswith(PARSED_PREFIX) for r in rows if r.key in gold),
    )
    return rep, fm


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Eligibility checker vs hand labels on live criteria text")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every false met")
    ap.add_argument("--parses", type=Path, metavar="FILE",
                    help="JSON {criterion_key: raw LLM parse} to refine the checker with (issue 56)")
    args = ap.parse_args(argv)
    fixtures, labels = load_fixtures(), load_labels()
    parses = None
    if args.parses is not None:
        parses = dict_source(json.loads(args.parses.read_text(encoding="utf-8")))
    results = [report(p, fixtures, labels, parses) for p in labels["profiles"]]
    if args.json:
        out = [asdict(rep) | {"false_mets": [asdict(f.row) | {"label": f.label} for f in fm]}
               for rep, fm in results]
        json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    print(f"{len(fixtures)} live trials, labels in {LABELS_FILE.name}")
    for rep, fm in results:
        print(f"\n== profile: {rep.profile} ==")
        print(f"false met: {rep.false_met}/{rep.checker_met} checker mets = {rep.false_met_rate:.1%}"
              f"   false not_met: {rep.false_not_met}"
              f"   agreement: {rep.agree}/{rep.bullets} = {rep.agreement_rate:.1%}")
        print("label -> checker:", rep.confusion)
        print(f"recall: {rep.label_met_as_met}/{rep.label_met} label-met bullets called met"
              f"   decided by parse: {rep.decided_by_parse}")
        print(f"not-met guard turned unknown: {rep.guard_unknown} "
              f"(labeled not_met: {rep.guard_unknown_labeled_not_met})")
        if args.verbose:
            for f in fm:
                r = f.row
                print(f"  {r.trial_id}[{r.index}] {r.criterion_type} label={f.label} got=met "
                      f"[{r.evidence_field}] {r.criterion_text[:100]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
