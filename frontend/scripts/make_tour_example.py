"""Regenerate the guided tour's example case (frontend/src/tutorial/exampleCase.ts).

Walks a synthetic patient (default DEFAULT_PATIENT) through a running backend
with its scripted doctor decisions (clinical/patients/<id>.json), stops at
review_shortlist, and writes the record and audit log as a typed TypeScript
fixture. Nothing is hand-written, so the tour shows what the real agent
produces.

The default, demo-016 (recurrent glioblastoma), is the only kind of case
that reaches the shortlist after the mutation check, so the tour shows every
stage. Its scripted enter_results leaves out recurrence_number,
can_use_phone_or_tablet and comorbidities, so the rules that need them stay
unknown ("doctor must enter it"), exactly as the app would show them.

Run the real backend with synthetic trials first, e.g.:

    CORTEXA_CLASSIFIER=real CORTEXA_TRIAL_SOURCE=demo \
    CORTEXA_DATA_DIR=<scratch>/data CORTEXA_CACHE_DIR=<scratch>/cache \
    python -m uvicorn agent.api:app --port 8765

then:

    python frontend/scripts/make_tour_example.py [--patient demo-0NN] [--base http://127.0.0.1:8765]

It uses a fresh random workspace, so it never touches anyone's cases.
Re-run it when CaseRecord or AuditEntry change; `npm run typecheck` fails on a
stale fixture.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "frontend" / "src" / "tutorial" / "exampleCase.ts"
DEFAULT_PATIENT = "demo-016"
DOCTOR = "dr-example"
GATE = {
    "confirm_bucket": "confirm_bucket",
    "preop_screen": "preop_review",
    "awaiting_pathology": "enter_results",
    "enter_results": "enter_results",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--patient", default=DEFAULT_PATIENT)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    source = args.patient
    # "~" is outside the backend's case ID pattern (agent/store.py), so no real
    # case can ever have this ID. The fixture exports it as TOUR_CASE_ID.
    tour_id = f"example~{source}"
    out_path: Path = args.out
    ws = "tourexample" + uuid.uuid4().hex[:12]

    def call(method: str, path: str, body=None, ctype: str = "application/json"):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            base + path, data=data, method=method, headers={"X-Cortexa-Workspace": ws, "Content-Type": ctype}
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} -> {e.code}: {e.read().decode()}")

    health = call("GET", "/health")
    if health.get("trial_source") != "demo":
        sys.exit("backend must run with CORTEXA_TRIAL_SOURCE=demo (synthetic trials only)")

    patient = json.loads((REPO / "clinical" / "patients" / f"{source}.json").read_text(encoding="utf-8"))
    image = (REPO / "classifier" / "demo_scans" / f"{source}.jpg").read_bytes()
    call("POST", f"/cases?case_id={source}&filename={source}.jpg", image, "image/jpeg")

    for _ in range(12):
        rec = call("GET", f"/cases/{source}/record")
        st = rec["state"]
        if st["stage"] == "review_shortlist":
            break
        if st["waiting_on"] == "agent":
            call("POST", f"/cases/{source}/run")
            continue
        gate = GATE[st["stage"]]
        decision = dict(patient["doctor_decisions"][gate])
        if gate == "preop_review":
            decision["screen_id"] = rec["preop"]["screen_id"]
        call("POST", f"/cases/{source}/decisions", {
            "case_id": source, "gate": gate, "decision": decision, "doctor_id": DOCTOR,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "note": None,
        })
    else:
        sys.exit(f"{source} did not reach review_shortlist")

    record = call("GET", f"/cases/{source}/record")
    audit = call("GET", f"/cases/{source}/audit")
    call("POST", "/workspace/reset")  # leave nothing behind on the backend

    # The heatmap is a file path on the backend machine; the tour shows the demo MRI only.
    record["classifier"]["heatmap_path"] = None

    def rename(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: rename(v) for k, v in x.items()}
        if isinstance(x, list):
            return [rename(v) for v in x]
        return tour_id if x == source else x

    record, audit = rename(record), rename(audit)
    text = json.dumps({"record": record, "audit": audit})
    for leak in (ws, "workspaces", ":\\", ":/", "/tmp/", "/home/"):
        if leak in text:
            sys.exit(f"fixture contains {leak!r}: scrub it before committing")

    out_path.write_text(
        "// GENERATED by frontend/scripts/make_tour_example.py. Do not edit by hand.\n"
        f"// Synthetic patient {source}, walked through the real backend with its scripted\n"
        "// doctor decisions and synthetic demo trials. Shown only during the guided tour,\n"
        "// never saved and never sent to the backend (see api.ts).\n"
        'import type { AuditEntry, CaseRecord } from "../types";\n\n'
        f"export const TOUR_CASE_ID = {json.dumps(tour_id)};\n"
        f"export const TOUR_SOURCE_PATIENT = {json.dumps(source)};\n\n"
        f"export const EXAMPLE_RECORD = {json.dumps(record, indent=2)} satisfies CaseRecord;\n\n"
        f"export const EXAMPLE_AUDIT = {json.dumps(audit, indent=2)} satisfies AuditEntry[];\n",
        encoding="utf-8",
    )
    print(f"wrote {out_path}: stage {record['state']['stage']}, "
          f"{len(record['shortlist']['trials'])} trials, {len(audit)} audit entries")


if __name__ == "__main__":
    main()
