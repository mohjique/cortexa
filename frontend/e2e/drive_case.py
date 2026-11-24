"""Walk one synthetic demo case through its doctor gates to the trial shortlist.

Uses the decisions recorded in clinical/patients/<case>.json, as a doctor would
enter them. Pre-surgery trials the screen lists are acknowledged as not pursued.

    python frontend/e2e/drive_case.py WORKSPACE demo-016 [--reset]
"""
import json
import sys
import time
import urllib.error
import urllib.request

from config import API, DOCTOR_ID, REPO

# Stage -> the gate a doctor submits there (agent/workflow.py GATE_AT_STAGE).
GATE_AT_STAGE = {"preop_screen": "preop_review", "awaiting_pathology": "enter_results"}


def call(ws: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method, headers={"X-Cortexa-Workspace": ws})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode()[:400]}")


def drive(ws: str, cid: str, reset: bool = False) -> dict:
    """Create the case from its demo scan and submit gates until the shortlist (or a stop)."""
    decisions = json.loads((REPO / "clinical" / "patients" / f"{cid}.json").read_text())["doctor_decisions"]
    if reset:
        call(ws, "POST", "/workspace/reset")
    img = (REPO / "classifier" / "demo_scans" / f"{cid}.jpg").read_bytes()
    call(ws, "POST", f"/cases?case_id={cid}&filename={cid}.jpg", raw=img)

    for _ in range(600):
        rec = call(ws, "GET", f"/cases/{cid}/record")
        stage = rec["state"]["stage"]
        if stage == "review_shortlist" or rec.get("no_match"):
            break
        # Pathology results arrive from the lab, so that stage isn't waiting on the doctor.
        if rec["state"].get("waiting_on") != "doctor" and stage != "awaiting_pathology":
            if stage in ("stopped", "rescan"):
                break
            time.sleep(1)
            continue
        gate = GATE_AT_STAGE.get(stage, stage)
        if gate not in decisions:
            break  # the patient's scripted path ends here
        d = dict(decisions[gate])
        if gate == "preop_review":
            d["screen_id"] = rec["preop"]["screen_id"]
            d["pre_surgery_trials"] = [
                {"trial_id": t["trial_id"], "action": "not_pursuing", "reason": "synthetic dry run"}
                for t in rec["preop"]["pre_surgery_trials"]
            ]
        call(ws, "POST", f"/cases/{cid}/decisions",
             {"case_id": cid, "gate": gate, "decision": d, "doctor_id": DOCTOR_ID,
              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    rec = call(ws, "GET", f"/cases/{cid}/record")
    sl = rec.get("shortlist") or {"trials": []}
    sites = [t for t in sl["trials"] if (t.get("nearest_site") or {}).get("lat") is not None]
    places = {(round(t["nearest_site"]["lat"], 4), round(t["nearest_site"]["lon"], 4)) for t in sites}
    print(f"{cid}: stage={rec['state']['stage']} trials={len(sl['trials'])} mapped={len(sites)} sites={len(places)}")
    return rec


if __name__ == "__main__":
    drive(sys.argv[1], sys.argv[2], reset="--reset" in sys.argv)
