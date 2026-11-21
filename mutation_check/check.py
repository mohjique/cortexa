"""Mutation check: is temozolomide's SBS11 signature detectable in a recurrent glioma's mutations?

One entry point for the agent::

    from mutation_check import run_mutation_check
    out = run_mutation_check(request, false_alarm_rate=policy.mutation_check.false_alarm_rate)

``request`` is the Mutation check input from ``docs/interfaces.md`` (``case_id``, ``sequencing``,
``reference``, ``snvs``). It may carry ``counts_96`` instead of ``snvs`` (proposed in issue #6), so the
demo patients' stored catalogs can be checked without inventing SNVs. The result is exactly a
``MutationCheckRan`` or ``MutationCheckSkipped`` (``agent/schemas.py``).

The detection engine is ``sigdetect`` (Poisson fit + likelihood-ratio test with per-N simulated
thresholds). When it isn't installed, the three demo catalogs still run from ``demo_results.json``,
precomputed by ``make_demo_results.py``, the same idea as ``agent/demo_cache/``. Anything else raises
``MutationCheckUnavailable``: the check never guesses.

The result is context only. It never decides eligibility, and ``total_mutations`` is not TMB.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

__all__ = ["BIN_ORDER", "MutationCheckUnavailable", "counts_from_snvs", "run_mutation_check", "snv_bin"]

HERE = Path(__file__).resolve().parent
NOTE = "Context only. Does not decide eligibility."

# COSMIC SBS96 row order: 5' base, then substitution, then 3' base.
SUBSTITUTIONS = ("C>A", "C>G", "C>T", "T>A", "T>C", "T>G")
BIN_ORDER = [f"{l}[{s}]{r}" for l in "ACGT" for s in SUBSTITUTIONS for r in "ACGT"]
_INDEX = {b: i for i, b in enumerate(BIN_ORDER)}
_COMPLEMENT = str.maketrans("ACGT", "TGCA")

# The engine's verdicts -> the contract's sbs11_call.
CALL = {"detected": "detected", "not_detected": "not_detected", "too_few_mutations": "inconclusive"}
BROAD = ("exome", "genome")


class MutationCheckUnavailable(RuntimeError):
    """The check can't give a trustworthy answer here (engine missing, or calibration mismatch)."""


def snv_bin(context: str, ref: str, alt: str) -> str:
    """The SBS96 bin of one SNV.

    ``context`` is the reference-strand trinucleotide with ``ref`` in the middle. A purine reference
    (G or A) is folded onto the pyrimidine strand by reverse-complementing context and change, because
    sequencing can't tell which strand was hit: G>A at CGA is C>T at TCG, bin T[C>T]G.
    """
    context, ref, alt = context.upper(), ref.upper(), alt.upper()
    if len(context) != 3 or set(context + ref + alt) - set("ACGT"):
        raise ValueError(f"bad SNV: context={context!r} ref={ref!r} alt={alt!r}")
    if context[1] != ref or ref == alt:
        raise ValueError(f"context middle base must equal ref, and alt must differ: {context} {ref}>{alt}")
    if ref in "GA":
        context = context.translate(_COMPLEMENT)[::-1]
        ref, alt = ref.translate(_COMPLEMENT), alt.translate(_COMPLEMENT)
    return f"{context[0]}[{ref}>{alt}]{context[2]}"


def counts_from_snvs(snvs: list[dict[str, Any]]) -> list[int]:
    """96 counts from SNV dicts. A variant listed twice (same chrom, pos, ref, alt) counts once."""
    counts = [0] * 96
    seen: set[tuple[str, int, str, str]] = set()
    for s in snvs:
        key = (str(s["chrom"]), int(s["pos"]), s["ref"].upper(), s["alt"].upper())
        if key in seen:
            continue
        seen.add(key)
        counts[_INDEX[snv_bin(s["context"], s["ref"], s["alt"])]] += 1
    return counts


def _counts(request: dict[str, Any]) -> list[int]:
    has_snvs, has_counts = "snvs" in request, "counts_96" in request
    if has_snvs == has_counts:
        raise ValueError("send exactly one of 'snvs' or 'counts_96'")
    if has_snvs:
        return counts_from_snvs(request["snvs"])
    c = request["counts_96"]
    # bool is a subclass of int in Python, so True/False must be excluded explicitly.
    if len(c) != 96 or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in c):
        raise ValueError("counts_96 must be 96 non-negative integers in COSMIC row order")
    return list(c)


def _digest(counts: list[int]) -> str:
    return hashlib.sha256(json.dumps(counts).encode()).hexdigest()[:16]


def _calibration_path() -> Path:
    """$SIGDETECT_CALIBRATION, else a sigdetect checkout next to this repo."""
    env = os.environ.get("SIGDETECT_CALIBRATION")
    return Path(env) if env else HERE.parents[1] / "sigdetect" / "data" / "sbs11_calibration.npz"


def _load_engine():
    """(Calibration, Detector) classes, or None when sigdetect or its calibration file is missing."""
    try:
        from sigdetect import Calibration, Detector
    except ImportError:
        return None
    if not _calibration_path().exists():
        return None
    return Calibration, Detector


def _report(case_id: str, counts: list[int], far: float) -> dict[str, Any]:
    engine = _load_engine()
    if engine is not None:
        Calibration, Detector = engine
        cal = Calibration.load(_calibration_path())
        if abs(cal.far - far) > 1e-12:
            raise MutationCheckUnavailable(
                f"calibrated at false-alarm rate {cal.far}, policy asks for {far}; rebuild the calibration "
                f"(sigdetect: python scripts/build_sbs11.py --far {far})")
        if cal.bin_labels and list(cal.bin_labels) != BIN_ORDER:
            raise MutationCheckUnavailable("calibration bins are not in COSMIC SBS96 order")
        return Detector(cal).report(counts)

    cached = json.loads((HERE / "demo_results.json").read_text(encoding="utf-8"))
    hit = cached["results"].get(case_id)
    if hit is None or hit["counts_sha"] != _digest(counts):
        raise MutationCheckUnavailable(
            "sigdetect is not installed and this catalog has no precomputed result; install sigdetect "
            "(see mutation_check/README.md)")
    if abs(cached["false_alarm_rate"] - far) > 1e-12:
        raise MutationCheckUnavailable(
            f"precomputed results are at false-alarm rate {cached['false_alarm_rate']}, policy asks for {far}")
    return hit["report"]


def _to_contract(case_id: str, rep: dict[str, Any], far: float, include_limits: bool) -> dict[str, Any]:
    if rep.get("verdict") not in CALL:
        raise MutationCheckUnavailable(f"engine returned an unknown verdict {rep.get('verdict')!r}")
    n = rep["n_mutations"]
    exp = {k: (v / n if n else 0.0) for k, v in rep["background_exposures"].items()}
    exp[rep["target"]] = rep["target_fraction"] if n else 0.0
    total = sum(exp.values())
    exposures = {k: round(min(max(v / total if total else 0.0, 0.0), 1.0), 4) for k, v in exp.items()}
    out: dict[str, Any] = {
        "case_id": case_id,
        "ran": True,
        "total_mutations": n,
        "sbs11_call": CALL[rep["verdict"]],
        "false_alarm_rate": far,
        "exposures": exposures,
        "note": NOTE,
    }
    if include_limits:  # issue #6: needs the four optional fields in agent/schemas.py first
        out.update(
            sbs11_fraction_ci95=[round(x, 4) for x in rep["ci95"]],
            critical_fraction=None if rep["critical_fraction"] is None else round(rep["critical_fraction"], 4),
            detection_limit_95=None if rep["detection_limit_95"] is None else round(rep["detection_limit_95"], 4),
            summary=rep["summary"],
        )
    return out


def run_mutation_check(request: dict[str, Any], *, false_alarm_rate: float,
                       include_limits: bool = False) -> dict[str, Any]:
    """Run the check for one case; returns a MutationCheckRan or MutationCheckSkipped dict.

    The agent decides whether a case is a recurrent glioma before calling. This function skips any
    case without exome or genome sequencing, because the false-alarm calibration doesn't hold for
    small panels. ``include_limits`` adds the four fields proposed in issue #6; leave it False until
    the schema has them (the schema rejects unknown keys).
    """
    case_id = request["case_id"]
    if request.get("sequencing") not in BROAD:
        return {"case_id": case_id, "ran": False, "skip_reason": "no_broad_sequencing"}
    counts = _counts(request)
    rep = _report(case_id, counts, false_alarm_rate)
    return _to_contract(case_id, rep, false_alarm_rate, include_limits)
