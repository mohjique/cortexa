"""Mutation check hook: builds the request from the case and calls `mutation_check`.

Runs only after `workflow.needs_mutation_check` says yes (recurrent glioma,
broad sequencing). The result is context only and never decides eligibility
(safety rule 10).

When the check can't give a trustworthy answer (no catalog on file, engine
missing, calibration mismatch, bad catalog), the hook audits why and returns
None. Matching then runs without the check, the same as `inconclusive`
(decision tree: "Inconclusive mutation check = no information"). A guessed
result never takes its place.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mutation_check import MutationCheckUnavailable, run_mutation_check

from agent.audit import audit
from agent.policy import Policy
from agent.schemas import (
    MutationCheckInput,
    MutationCheckOutput,
    MutationCheckOutputAdapter,
)
from agent.store import CaseRecord

REFERENCE = "GRCh38"
PATIENTS_DIR = Path(__file__).resolve().parents[1] / "clinical" / "patients"
_DEMO_ID = re.compile(r"demo-\d{3}")

# A catalog is {"sequencing": ..., and exactly one of "counts_96" or "snvs"}.
Catalog = dict[str, Any]
CatalogSource = Callable[[str], Catalog | None]


def demo_catalog(case_id: str) -> Catalog | None:
    """The stored catalog for a synthetic demo patient, or None.

    Reads only `broad_sequencing` and `mutation_counts_96`. The answer key
    (`true_sbs11_fraction`) is never read.
    """
    if not _DEMO_ID.fullmatch(case_id):
        return None
    path = PATIENTS_DIR / f"{case_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = data.get("mutation_counts_96")
    if counts is None:
        return None
    return {"sequencing": data.get("broad_sequencing"), "counts_96": counts}


# Real use will add an SNV source (sequencing upload). Swap with set_catalog_source.
_catalog_source: CatalogSource = demo_catalog


def set_catalog_source(source: CatalogSource) -> CatalogSource:
    """Swap where catalogs come from (tests, real SNV uploads). Returns the old source."""
    global _catalog_source
    old, _catalog_source = _catalog_source, source
    return old


def _unavailable(case_id: str, reason: str) -> None:
    audit("agent", "mutation_check_unavailable", {"reason": reason}, case_id=case_id)
    return None


def build_request(rec: CaseRecord, catalog: Catalog) -> dict[str, Any]:
    """The mutation check input, validated, with the unused source key left out."""
    assert rec.results is not None
    # Doctor-entered sequencing wins; else the assay recorded with the catalog itself.
    sequencing = rec.results.sequencing or catalog.get("sequencing")
    req = MutationCheckInput.model_validate(
        {
            "case_id": rec.case_id,
            "sequencing": sequencing,
            "reference": REFERENCE,
            "snvs": catalog.get("snvs"),
            "counts_96": catalog.get("counts_96"),
        }
    )
    return req.model_dump(mode="json", exclude_none=True)


def mutation_check_hook(rec: CaseRecord, policy: Policy) -> MutationCheckOutput | None:
    """Run the real check. None = no trustworthy answer (already audited)."""
    cid = rec.case_id
    catalog = _catalog_source(cid)
    if catalog is None:
        return _unavailable(cid, "no mutation catalog on file for this case")
    try:
        request = build_request(rec, catalog)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        return _unavailable(cid, f"invalid mutation catalog: {exc}")

    far = policy.mutation_check.false_alarm_rate
    source = "snvs" if "snvs" in request else "counts_96"
    audit(
        "agent",
        "tool_call:mutation_check",
        {"sequencing": request["sequencing"], "source": source, "false_alarm_rate": far},
        case_id=cid,
    )
    try:
        out = run_mutation_check(request, false_alarm_rate=far)
    except MutationCheckUnavailable as exc:
        return _unavailable(cid, str(exc))
    except ValueError as exc:
        return _unavailable(cid, f"invalid mutation catalog: {exc}")

    result = MutationCheckOutputAdapter.validate_python(out)
    if result.case_id != cid:
        raise ValueError(f"mutation check returned case_id {result.case_id!r}, expected {cid!r}")
    return result
