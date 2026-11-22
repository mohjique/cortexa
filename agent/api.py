"""FastAPI backend for case state and doctor decisions (F1.5, F3.1).

Run: uvicorn agent.api:app --reload
"""

from __future__ import annotations

import io
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError

from agent import workflow
from agent.audit import DATA_DIR, audit, get_audit_log
from agent.policy import get_policy
from agent.schemas import AuditEntry, CaseState, DoctorDecisionAdapter, NonEmptyStr, Schema
from agent.store import CaseExists, CaseNotFound, CaseRecord, get_store

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
HEATMAP_DIR = DATA_DIR / "heatmaps"
# The uploaded MRI, re-encoded as PNG, so the doctor can read the scan itself.
SCAN_DIR = DATA_DIR / "scans"
# The 23 demo MRIs (public Kaggle dataset), committed and named by case ID only.
DEMO_SCANS_DIR = Path(__file__).resolve().parents[1] / "classifier" / "demo_scans"
_DEMO_SCAN = re.compile(r"^(demo-\d{3})\.(jpg|jpeg|png)$")

# ---------------------------------------------------------------------------
# Classifier: CORTEXA_CLASSIFIER=real | stub. Default: real if model.pt exists.
# ---------------------------------------------------------------------------

_classifier: Any = None


def get_classifier() -> Any:
    global _classifier
    if _classifier is None:
        from classifier.real import DEFAULT_MODEL

        choice = os.environ.get("CORTEXA_CLASSIFIER") or ("real" if DEFAULT_MODEL.exists() else "stub")
        if choice == "real":
            from classifier.real import ResNetClassifier

            _classifier = ResNetClassifier(heatmap_dir=HEATMAP_DIR)
        else:
            from classifier.stub import StubClassifier

            _classifier = StubClassifier()
        audit("system", "classifier_loaded", {"classifier": type(_classifier).__name__})
    return _classifier


def set_classifier(c: Any) -> None:
    """Swap the classifier (tests)."""
    global _classifier
    _classifier = c


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_policy()  # fail fast on a bad policy file
    yield


app = FastAPI(title="Cortexa agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORTEXA_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _get(case_id: str) -> CaseRecord:
    try:
        return get_store().get(case_id)
    except CaseNotFound:
        raise HTTPException(404, f"case {case_id} not found") from None
    except ValueError as exc:  # bad case_id format
        raise HTTPException(422, str(exc)) from None


@app.get("/cases", response_model=list[CaseState])
def list_cases() -> list[CaseState]:
    return get_store().list_cases()


@app.post("/cases", response_model=CaseState, status_code=201)
async def create_case(request: Request, case_id: str | None = None, filename: str | None = None) -> CaseState:
    """Upload an MRI image (raw bytes as the request body) and classify it.

    `fetch("/cases?case_id=demo-016", {method: "POST", body: file})`.
    Creates the case, runs the classifier and moves it to confirm_bucket.
    The result is a suspected bucket only; the doctor confirms (rules 1, 2).
    Synthetic or public-dataset images only (rule 9). The image is kept, as a
    PNG we re-encode, so the doctor can view it (GET /cases/{id}/image).
    """
    image = await request.body()
    if not image:
        raise HTTPException(422, "empty upload: send the image bytes as the request body")
    if len(image) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image larger than 20 MB")
    case_id = case_id or f"case-{uuid.uuid4().hex[:8]}"
    store = get_store()
    try:
        store.get(case_id)
        raise HTTPException(409, f"case {case_id} already exists")
    except CaseNotFound:
        pass
    except ValueError as exc:  # bad case_id format
        raise HTTPException(422, str(exc)) from None
    # Classify before creating the case, so a bad upload leaves nothing behind.
    clf = get_classifier()
    try:
        output = clf.classify(case_id, image, filename)
    except ValueError as exc:  # BadImage, or a schema error from the classifier
        audit("agent", "classification_failed", {"error": str(exc)}, case_id=case_id)
        raise HTTPException(422, f"could not classify: {exc}") from None
    try:
        store.create(case_id)
    except CaseExists:
        raise HTTPException(409, f"case {case_id} already exists") from None
    audit("agent", "tool_call:classifier",
          {"classifier": type(clf).__name__, "suspected_bucket": output.suspected_bucket},
          case_id=case_id)
    audit("agent", "scan_stored", {"stored": _save_scan(case_id, image)}, case_id=case_id)
    return workflow.attach_classification(case_id, output).state


def _save_scan(case_id: str, image: bytes) -> bool:
    """Keep the upload for the doctor's viewer, decoded and re-encoded as PNG.

    Only pixels we decoded are ever served back, never the uploaded bytes. An
    upload PIL can't read (the stub accepts any bytes) is not kept; the case
    still works, just without a scan to view. `case_id` is already validated
    by the store, so it is safe as a file name.
    """
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(image))
        img.load()
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
        SCAN_DIR.mkdir(parents=True, exist_ok=True)
        img.save(SCAN_DIR / f"{case_id}.png", format="PNG")
        return True
    except Exception:  # noqa: BLE001 - a scan we can't keep must not fail the upload
        return False


@app.get("/cases/{case_id}/image")
def get_scan(case_id: str) -> FileResponse:
    """The case's MRI as uploaded (re-encoded PNG). 404 if it was not kept."""
    _get(case_id)
    p = SCAN_DIR / f"{case_id}.png"
    if not p.is_file():
        raise HTTPException(404, "no scan stored for this case")
    return FileResponse(p, media_type="image/png")


def _demo_scans() -> dict[str, Path]:
    if not DEMO_SCANS_DIR.is_dir():
        return {}
    found = {}
    for p in sorted(DEMO_SCANS_DIR.iterdir()):
        if m := _DEMO_SCAN.match(p.name):
            found[m.group(1)] = p
    return found


class DemoScan(Schema):
    case_id: NonEmptyStr  # nothing else: no label or model call, so the gallery can't give the answer away


@app.get("/demo-scans", response_model=list[DemoScan])
def list_demo_scans() -> list[DemoScan]:
    """The demo MRIs a doctor can open as a case. Case IDs only."""
    return [DemoScan(case_id=cid) for cid in _demo_scans()]


@app.get("/demo-scans/{case_id}")
def get_demo_scan(case_id: str) -> FileResponse:
    """One demo MRI, unchanged, to view or to upload with POST /cases."""
    p = _demo_scans().get(case_id)  # exact key lookup: no path built from user input
    if p is None:
        raise HTTPException(404, f"no demo scan {case_id}")
    return FileResponse(p, media_type="image/png" if p.suffix == ".png" else "image/jpeg")


@app.get("/cases/{case_id}/heatmap")
def get_heatmap(case_id: str) -> FileResponse:
    """The Grad-CAM overlay for the case's classification (PNG)."""
    rec = _get(case_id)
    path = rec.classifier.heatmap_path if rec.classifier else None
    if not path:
        raise HTTPException(404, "no heatmap for this case")
    p = Path(path).resolve()
    if HEATMAP_DIR.resolve() not in p.parents or not p.is_file():  # only serve our own heatmaps
        raise HTTPException(404, "no heatmap for this case")
    return FileResponse(p, media_type="image/png")


@app.get("/cases/{case_id}", response_model=CaseState)
def get_case(case_id: str) -> CaseState:
    """Case state. The frontend picks its screen from `stage`."""
    return _get(case_id).state


@app.get("/cases/{case_id}/record", response_model=CaseRecord)
def get_record(case_id: str) -> CaseRecord:
    """Everything known about the case: classifier, decisions, screen, shortlist."""
    return _get(case_id)


@app.get("/cases/{case_id}/audit", response_model=list[AuditEntry])
def get_audit(case_id: str) -> list[AuditEntry]:
    _get(case_id)
    return get_audit_log().read(case_id)


@app.post("/cases/{case_id}/decisions", response_model=CaseState)
def post_decision(case_id: str, body: dict[str, Any] = Body(...)) -> CaseState:
    """Submit a doctor decision for the gate at the case's current stage.

    409 if the gate doesn't match the stage. 422 if the decision is malformed
    or conflicts with the case.
    """
    _get(case_id)
    try:
        decision = DoctorDecisionAdapter.validate_python(body)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from None
    if decision.case_id != case_id:
        raise HTTPException(422, "body case_id does not match the URL")
    try:
        return workflow.submit_decision(decision).state
    except workflow.WrongGate as exc:
        raise HTTPException(409, str(exc)) from None
    except workflow.InvalidDecision as exc:
        raise HTTPException(422, str(exc)) from None


class RescreenRequest(Schema):
    doctor_id: NonEmptyStr
    reason: NonEmptyStr | None = None


@app.post("/cases/{case_id}/preop/rescreen", response_model=CaseState)
def rescreen_preop(case_id: str, body: RescreenRequest) -> CaseState:
    """Doctor asks for a fresh pre-op screen. 409 unless the case waits on preop_review."""
    _get(case_id)
    try:
        return workflow.rescreen_preop(case_id, body.doctor_id, body.reason).state
    except workflow.WrongGate as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/cases/{case_id}/run", response_model=CaseState)
def run_agent(case_id: str) -> CaseState:
    """Retry agent stages after a failed hook. A no-op at doctor stages.

    503 if the stage fails again; the case is unchanged and can be retried.
    """
    _get(case_id)
    try:
        return workflow.run_agent(case_id).state
    except Exception as exc:  # hook failure, already audited as stage_failed
        raise HTTPException(503, f"agent stage failed, retry later: {type(exc).__name__}") from None
