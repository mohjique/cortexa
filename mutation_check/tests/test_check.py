"""The mutation check's output always validates against agent/schemas.py, and it never guesses."""

import json
from pathlib import Path

import pytest

from agent import schemas
from mutation_check import BIN_ORDER, MutationCheckUnavailable, counts_from_snvs, run_mutation_check, snv_bin
from mutation_check import check

HERE = Path(__file__).resolve().parents[1]
DEMO = json.loads((HERE / "demo_cases.json").read_text(encoding="utf-8"))
FAR = 0.01


def _request(case_id, **extra):
    c = DEMO["cases"][case_id]
    return {"case_id": case_id, "sequencing": c["broad_sequencing"], "reference": "GRCh38",
            "counts_96": c["mutation_counts_96"], **extra}


def _valid(out):
    """Parse with the agent's own schema (extra='forbid'); returns the parsed model."""
    return schemas.MutationCheckOutputAdapter.validate_python(out)


@pytest.fixture
def no_engine(monkeypatch):
    """Pretend sigdetect isn't installed, as on a teammate's laptop."""
    monkeypatch.setattr(check, "_load_engine", lambda: None)


# ---- the 96 bins -------------------------------------------------------------------------------

def test_bin_order_is_cosmic_order():
    assert BIN_ORDER == DEMO["bin_order"]
    assert BIN_ORDER[0] == "A[C>A]A" and BIN_ORDER[-1] == "T[T>G]T" and len(set(BIN_ORDER)) == 96


@pytest.mark.parametrize("context,ref,alt,expected", [
    ("ACG", "C", "T", "A[C>T]G"),   # pyrimidine reference: as is (a CpG C>T)
    ("CGA", "G", "A", "T[C>T]G"),   # purine: reverse-complemented onto the C strand
    ("TAC", "A", "G", "G[T>C]A"),
    ("acg", "c", "t", "A[C>T]G"),   # case-insensitive
])
def test_snv_bin_folds_to_pyrimidine(context, ref, alt, expected):
    assert snv_bin(context, ref, alt) == expected


@pytest.mark.parametrize("context,ref,alt", [("AAG", "C", "T"), ("ACG", "C", "C"), ("ANG", "N", "T")])
def test_snv_bin_rejects_bad_snvs(context, ref, alt):
    with pytest.raises(ValueError):
        snv_bin(context, ref, alt)


def test_counts_from_snvs_dedupes_and_folds():
    snvs = [
        {"chrom": "chr7", "pos": 55191822, "ref": "C", "alt": "T", "context": "ACG"},
        {"chrom": "chr7", "pos": 55191822, "ref": "C", "alt": "T", "context": "ACG"},  # duplicate
        {"chrom": "chr1", "pos": 100, "ref": "G", "alt": "A", "context": "CGT"},       # -> A[C>T]G
    ]
    c = counts_from_snvs(snvs)
    assert sum(c) == 2 and c[BIN_ORDER.index("A[C>T]G")] == 2


# ---- skip rules ----------------------------------------------------------------------------------

@pytest.mark.parametrize("seq", ["panel", None])
def test_panel_or_missing_sequencing_is_skipped(seq):
    out = run_mutation_check({"case_id": "demo-x", "sequencing": seq, "reference": "GRCh38", "snvs": []},
                             false_alarm_rate=FAR)
    assert out == {"case_id": "demo-x", "ran": False, "skip_reason": "no_broad_sequencing"}
    _valid(out)


def test_exactly_one_of_snvs_or_counts():
    req = _request("demo-016", snvs=[])
    with pytest.raises(ValueError, match="exactly one"):
        run_mutation_check(req, false_alarm_rate=FAR)


# ---- demo cases without the engine (precomputed) --------------------------------------------------

@pytest.mark.parametrize("case_id", ["demo-016", "demo-017", "demo-018"])
def test_demo_cases_without_engine(no_engine, case_id):
    out = run_mutation_check(_request(case_id), false_alarm_rate=FAR)
    parsed = _valid(out)
    assert parsed.ran is True
    assert out["sbs11_call"] == DEMO["cases"][case_id]["expected_sbs11_call"]
    assert out["total_mutations"] == sum(DEMO["cases"][case_id]["mutation_counts_96"])
    assert abs(sum(out["exposures"].values()) - 1) < 1e-3
    assert out["note"] == check.NOTE


def test_unknown_catalog_without_engine_raises(no_engine):
    req = _request("demo-016")
    req["counts_96"] = [1] * 96
    with pytest.raises(MutationCheckUnavailable):
        run_mutation_check(req, false_alarm_rate=FAR)


def test_far_mismatch_without_engine_raises(no_engine):
    with pytest.raises(MutationCheckUnavailable, match="false-alarm rate"):
        run_mutation_check(_request("demo-016"), false_alarm_rate=0.05)


def test_include_limits_adds_the_issue_6_fields(no_engine):
    out = run_mutation_check(_request("demo-018"), false_alarm_rate=FAR, include_limits=True)
    assert out["sbs11_fraction_ci95"][0] == 0.0 and out["sbs11_fraction_ci95"][1] < 0.2
    assert out["detection_limit_95"] > out["critical_fraction"] > 0
    assert out["summary"].startswith("SBS11 was not detected")
    # Issue #6 landed: the agent's schema accepts the four fields.
    parsed = _valid(out)
    assert isinstance(parsed, schemas.MutationCheckRan) and parsed.summary == out["summary"]


@pytest.mark.parametrize("case_id", ["demo-016", "demo-017", "demo-018"])
def test_include_limits_validates_for_every_demo_case(no_engine, case_id):
    req = _request(case_id)
    schemas.MutationCheckInput.model_validate(req)  # counts_96 input is part of the contract now
    _valid(run_mutation_check(req, false_alarm_rate=FAR, include_limits=True))


# ---- live engine (only where sigdetect is installed) -----------------------------------------------

live = pytest.mark.skipif(check._load_engine() is None, reason="sigdetect or its calibration not installed")


@live
@pytest.mark.parametrize("case_id", ["demo-016", "demo-017", "demo-018"])
def test_live_engine_matches_precomputed(case_id, monkeypatch):
    live_out = run_mutation_check(_request(case_id), false_alarm_rate=FAR)
    monkeypatch.setattr(check, "_load_engine", lambda: None)
    assert run_mutation_check(_request(case_id), false_alarm_rate=FAR) == live_out
    _valid(live_out)


@live
def test_live_engine_on_snvs_matches_counts():
    """SNVs that fold to demo-018's catalog give the same answer as its counts."""
    counts = DEMO["cases"]["demo-018"]["mutation_counts_96"]
    snvs, pos = [], 1
    for b, n in zip(BIN_ORDER, counts):
        for _ in range(n):
            snvs.append({"chrom": "chr1", "pos": pos, "ref": b[2], "alt": b[4], "context": b[0] + b[2] + b[-1]})
            pos += 1
    a = run_mutation_check({"case_id": "demo-018", "sequencing": "exome", "reference": "GRCh38", "snvs": snvs},
                           false_alarm_rate=FAR)
    assert a == run_mutation_check(_request("demo-018"), false_alarm_rate=FAR)


@live
def test_live_far_mismatch_raises():
    with pytest.raises(MutationCheckUnavailable, match="false-alarm rate"):
        run_mutation_check(_request("demo-016"), false_alarm_rate=0.05)
