"""Regenerate the guided tour's example case (frontend/src/tutorial/exampleCase.ts and tourCase.ts).

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

It uses a fresh random workspace, so it never touches anyone's cases, and
resets it at the end (the empty workspace expires with the backend's TTL).
Re-run it when CaseRecord or AuditEntry change; `npm run typecheck` fails on a
stale fixture.
"""

import argparse
import json
import os
import re
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
# Mirrors GATE_AT_STAGE in agent/workflow.py, minus review_shortlist (the walk
# stops there). Any other stage is the end of the case's path.
GATE = {
    "confirm_bucket": "confirm_bucket",
    "watch_or_treat": "watch_or_treat",
    "preop_screen": "preop_review",
    "hormone_labs": "hormone_labs",
    "surgery_or_meds": "surgery_or_meds",
    "awaiting_pathology": "enter_results",
    "enter_results": "enter_results",
}
# How the tour names a confirmed bucket (a shortlist case always has a tumor bucket).
BUCKET_TEXT = {"glioma": "glioma", "meningioma": "meningioma", "pituitary": "pituitary tumor"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--patient", default=DEFAULT_PATIENT)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    source = args.patient
    # "~" is outside the backend's case ID pattern (agent/store.py), so no real
    # case can ever have this ID. tourCase.ts exports it as TOUR_CASE_ID.
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
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            sys.exit(f"{method} {path}: can't reach the backend at {base} ({getattr(e, 'reason', e)})")

    health = call("GET", "/health")
    if health.get("trial_source") != "demo":
        sys.exit("backend must run with CORTEXA_TRIAL_SOURCE=demo (synthetic trials only)")

    patient = json.loads((REPO / "clinical" / "patients" / f"{source}.json").read_text(encoding="utf-8"))
    scan = REPO / "classifier" / "demo_scans" / f"{source}.jpg"
    image = scan.read_bytes()

    # However the walk ends, reset the workspace so its case is deleted. The
    # backend can only reset a workspace, not delete it: an empty
    # tourexample… workspace stays until its TTL (agent/workspace.py) runs out.
    try:
        call("POST", f"/cases?case_id={source}&filename={source}.jpg", image, "image/jpeg")
        for _ in range(12):
            rec = call("GET", f"/cases/{source}/record")
            st = rec["state"]
            if st["stage"] == "review_shortlist":
                break
            # FINAL_STAGES in agent/workflow.py. Checked before waiting_on: no_match waits on the agent (alerts).
            if st["stage"] in ("stopped", "rescan", "no_match"):
                sys.exit(f"{source} ended at {st['stage']}, not review_shortlist: the tour needs a case with a shortlist")
            if st["waiting_on"] == "agent":
                call("POST", f"/cases/{source}/run")
                continue
            gate = GATE.get(st["stage"])
            if gate is None:
                sys.exit(f"{source} stopped at unexpected stage {st['stage']} (waiting on {st['waiting_on']})")
            if gate not in patient["doctor_decisions"]:
                sys.exit(f"{source} waits on {gate}, but {source}.json has no scripted {gate} decision")
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
    finally:
        try:
            call("POST", "/workspace/reset")
        except SystemExit as e:
            print(f"warning: could not reset workspace {ws}: {e}", file=sys.stderr)

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
    for leak in (ws, "workspaces", "/tmp/", "/home/"):
        if leak in text:
            sys.exit(f"fixture contains {leak!r}: scrub it before committing")
    # A Windows drive path (C:\ or C:/). The lookbehind skips URLs ("https://").
    drive = re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", text)
    if drive:
        sys.exit(f"fixture contains a local path ({drive.group()!r}): scrub it before committing")

    # What the tour says about the case, from what the doctor entered only
    # (confirmed bucket and recurrence), never the model's call.
    bucket = record["state"]["confirmed_bucket"]
    recurrent = bool((record.get("results") or {}).get("recurrent"))
    summary = f"a {'recurrent' if recurrent else 'new'} {BUCKET_TEXT[bucket]}"

    header = "// GENERATED by frontend/scripts/make_tour_example.py. Do not edit by hand.\n"
    # The IDs live in their own small module so the app can import them
    # everywhere while the full fixture loads only when the tour opens it.
    # The MRI is the committed demo scan, bundled so the tour needs no backend.
    ids_path = out_path.with_name("tourCase.ts")
    scan_import = Path(os.path.relpath(scan, ids_path.parent)).as_posix()
    ids_path.write_text(
        header
        + "// The guided tour's example case: IDs, a doctor-entered summary and the demo MRI.\n"
        "// The record and audit are in exampleCase.ts.\n"
        f"import scan from {json.dumps(scan_import)};\n\n"
        f"export const TOUR_CASE_ID = {json.dumps(tour_id)};\n"
        f"export const TOUR_SOURCE_PATIENT = {json.dumps(source)};\n"
        f"/** From the doctor-confirmed bucket and recurrence, never the model's call. */\n"
        f"export const TOUR_SOURCE_SUMMARY = {json.dumps(summary)};\n"
        "export const TOUR_SCAN_URL: string = scan;\n",
        encoding="utf-8",
    )
    out_path.write_text(
        header
        + f"// Synthetic patient {source}, walked through the real backend with its scripted\n"
        "// doctor decisions and synthetic demo trials. Shown only during the guided tour,\n"
        "// never saved and never sent to the backend (see api.ts, which loads it lazily).\n"
        'import type { AuditEntry, CaseRecord } from "../types";\n\n'
        f"export const EXAMPLE_RECORD = {json.dumps(record, indent=2)} satisfies CaseRecord;\n\n"
        f"export const EXAMPLE_AUDIT = {json.dumps(audit, indent=2)} satisfies AuditEntry[];\n",
        encoding="utf-8",
    )
    print(f"wrote {ids_path} and {out_path}: stage {record['state']['stage']}, "
          f"{len(record['shortlist']['trials'])} trials, {len(audit)} audit entries")


if __name__ == "__main__":
    main()
