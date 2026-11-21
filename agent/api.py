"""FastAPI backend for case state and doctor decisions (F1.5, F3.1).

Run: uvicorn agent.api:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from agent import workflow
from agent.audit import get_audit_log
from agent.policy import get_policy
from agent.schemas import AuditEntry, CaseState, DoctorDecisionAdapter, NonEmptyStr, Schema
from agent.store import CaseNotFound, CaseRecord, get_store


@asynccontextmanager
async def lifespan(_: FastAPI):
    workflow.check_gate_policy(get_policy())
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
