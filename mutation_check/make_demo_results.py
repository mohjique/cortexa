"""Precompute the engine's reports for the demo catalogs, so the app runs them without sigdetect installed.

    python -m mutation_check.make_demo_results      # needs sigdetect + its calibration; writes demo_results.json

Each entry pins the catalog's hash, so a changed catalog never picks up a stale result.
"""

import json
from pathlib import Path

from .check import CALL, _calibration_path, _digest, _load_engine

HERE = Path(__file__).resolve().parent


def main() -> None:
    engine = _load_engine()
    if engine is None:
        raise SystemExit(f"needs sigdetect and its calibration at {_calibration_path()}")
    Calibration, Detector = engine
    cal = Calibration.load(_calibration_path())
    det = Detector(cal)
    cases = json.loads((HERE / "demo_cases.json").read_text(encoding="utf-8"))["cases"]
    results = {}
    for case_id, c in cases.items():
        rep = det.report(c["mutation_counts_96"])
        call = CALL[rep["verdict"]]
        if call != c["expected_sbs11_call"]:
            raise SystemExit(f"{case_id}: engine says {call}, demo_cases.json expects {c['expected_sbs11_call']}")
        results[case_id] = {"counts_sha": _digest(c["mutation_counts_96"]), "report": rep}
        print(f"{case_id}: {call}")
    out = {"_about": "Precomputed sigdetect reports for demo_cases.json (make_demo_results.py). Synthetic.",
           "false_alarm_rate": cal.far, "results": results}
    (HERE / "demo_results.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("wrote", HERE / "demo_results.json")


if __name__ == "__main__":
    main()
