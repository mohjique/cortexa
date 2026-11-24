"""Run the full shortlist-map browser suite against a running backend + Vite.

    python frontend/e2e/run_suite.py --mode demo   # backend with the synthetic demo cache
    python frontend/e2e/run_suite.py --mode live   # backend with CORTEXA_TRIAL_SOURCE=live

Each run uses a fresh workspace, walks the synthetic demo cases to their
shortlists, then runs every check. Exits 1 if any check fails.

demo: small shortlists (1-3 trials, one site 0 mi from the patient), switching
      cases without a reload, phone width, and the read-only cards after review.
live: 33-95 pins, phone width, boundary variants (exactly 10 / 11 pins, a shared
      pin, no coordinates, one site, mixed), and the pin/keyboard extras.
"""
import argparse
import json
import sys
import time
import urllib.request

import map_checks
import map_extras
from config import API
from drive_case import drive

DEMO_CASES = ["demo-007", "demo-008", "demo-016", "demo-018"]
LIVE_CASES = ["demo-007", "demo-008", "demo-022", "demo-014", "demo-016"]
VARIANTS = ["ten", "eleven", "ten_plus_shared", "none", "onesite", "mixed"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["demo", "live"], required=True)
    mode = ap.parse_args().mode

    with urllib.request.urlopen(f"{API}/health", timeout=30) as r:
        source = json.loads(r.read())["trial_source"]
    if source != mode:
        print(f"Backend at {API} serves trial_source={source!r}; restart it for --mode {mode} "
              f"({'CORTEXA_TRIAL_SOURCE=live' if mode == 'live' else 'without CORTEXA_TRIAL_SOURCE'}).")
        return 2

    ws = f"e2e-{mode}-{time.strftime('%Y%m%d%H%M%S')}"
    cases = DEMO_CASES if mode == "demo" else LIVE_CASES
    print(f"workspace {ws}")
    for i, cid in enumerate(cases):
        drive(ws, cid, reset=(i == 0))

    results: list[tuple[str, int]] = []
    if mode == "demo":
        results.append(("small cases + switch", map_checks.run(ws, cases, switch=("demo-016", "demo-008"))))
        results.append(("phone", map_checks.run(ws, ["demo-016"], phone=True)))
        results.append(("review then read-only", map_checks.run(ws, [], review="demo-007")))
    else:
        results.append(("real cases", map_checks.run(ws, cases)))
        results.append(("phone", map_checks.run(ws, ["demo-022", "demo-007", "demo-014"], phone=True)))
        for v in VARIANTS:
            results.append((f"variant {v}", map_checks.run(ws, ["demo-007"], variant=v)))
        results.append(("pins + keyboard extras", map_extras.run(ws, "demo-016")))

    print("\nSUMMARY")
    for name, fails in results:
        print(f"  {'ok  ' if not fails else 'FAIL'} {name}" + (f" ({fails} failed)" if fails else ""))
    total = sum(f for _, f in results)
    print(f"{total} failure(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
